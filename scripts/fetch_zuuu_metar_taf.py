#!/usr/bin/env python3
"""Read-only fetcher for ZUUU METAR/TAF reports from AMSC."""

from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import math
import os
import pwd
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_source_acquisition_manager import build_source_acquisition_trace, utc_now_iso
from weather_fact_snapshot_writer import build_fact_snapshot_write_plan
from amsc_raw_history_archive import archive_amsc_poll, attach_archive_metadata
from amsc_taf_revision_runtime import parse_zuuu_taf_revision


ENDPOINT = "https://www.amsc.net.cn/gateway/api/saas/rest/amc/ReportController/messageRetrievalSort"
AIRPORT = "ZUUU"
REPORT_TYPES = {"METAR", "SPECI", "TAF"}
QUERY_TT = "SA,SP,FC,FT,"
PARSER_VERSION = "amsc_zuuu_metar_taf_fetcher_v1"
AMSC_LATEST_REPOINT_BLOCK_REASON = "source_acquisition_fact_cannot_repoint_posterior_ssot_latest"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
TOKEN_RE = re.compile(
    rb"\d{1,12}\$\$[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
OBSERVATION_TIME_RE = re.compile(r"\b(?:METAR|SPECI)\s+ZUUU\s+(\d{2})(\d{2})(\d{2})Z\b")
TAF_TIME_RE = re.compile(r"\bTAF(?:\s+(?:AMD|COR))?\s+ZUUU\s+(\d{2})(\d{2})(\d{2})Z\b")
METAR_TEMP_RE = re.compile(r"(?<!TX)(?<!TN)\b(M?\d{2}|//)/(M?\d{2}|//)\b")
TAF_TX_RE = re.compile(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b")
METAR_WIND_RE = re.compile(r"\b(?P<direction>\d{3}|VRB)(?P<speed>\d{2,3})(?:G(?P<gust>\d{2,3}))?(?P<unit>MPS|KT)\b")
METAR_WIND_VARIATION_RE = re.compile(r"\b(?P<from>\d{3})V(?P<to>\d{3})\b")
METAR_QNH_RE = re.compile(r"\bQ(\d{4})\b")
METAR_ALTIMETER_RE = re.compile(r"\bA(\d{4})\b")
METAR_CLOUD_RE = re.compile(r"\b(?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?\b|\b(?:CAVOK|NSC|SKC|CLR)\b")
METAR_WEATHER_RE = re.compile(
    r"\b(?:-|\+|VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
    r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)\b"
)
METAR_TREND_START_RE = re.compile(r"\s+(?=(?:BECMG|TEMPO)\b)")
TEMPERATURE_MOMENTUM_WINDOWS_HOURS = {
    "30m": 0.5,
    "1h": 1.0,
    "90m": 1.5,
    "2h": 2.0,
    "150m": 2.5,
    "3h": 3.0,
    "210m": 3.5,
    "4h": 4.0,
    "270m": 4.5,
    "5h": 5.0,
    "330m": 5.5,
    "6h": 6.0,
    "12h": 12.0,
    "24h": 24.0,
}
AMSC_MOMENTUM_NODE_TOLERANCE_SECONDS = 5 * 60
METAR_INTEGER_QUANTIZATION_NOISE_C = 0.5
METAR_ADJACENT_STEP_NOISE_C = 1.0
METAR_PLATFORM_WITHIN_ROLLING_MAX_C = 1.0
PEAK_WINDOW_LEAD_HOURS = 1.0
PEAK_WINDOW_POST_HOURS = 2.0


class FetchHardTimeout(RuntimeError):
    """Raised when one source fetch exceeds the poller's wall-clock budget."""


@contextlib.contextmanager
def fetch_hard_timeout(seconds: int):
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def _raise_timeout(_signum, _frame):
        raise FetchHardTimeout(f"AMSC fetch exceeded hard timeout {seconds}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def chrome_profile_roots() -> list[Path]:
    """Resolve Chrome roots across the root LaunchDaemon/user boundary."""

    roots: list[Path] = []
    configured = os.environ.get("AMSC_CHROME_PROFILE_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())

    homes = [Path.home()]
    try:
        owner_home = Path(pwd.getpwuid(ROOT.stat().st_uid).pw_dir)
    except (KeyError, OSError):
        owner_home = None
    if owner_home is not None and owner_home not in homes:
        homes.append(owner_home)
    for home in homes:
        roots.extend(
            [
                home / "Library/Application Support/Google/Chrome",
                home / "Library/Application Support/Chromium",
            ]
        )
    return list(dict.fromkeys(roots))


def chrome_leveldb_dirs() -> list[Path]:
    roots = chrome_profile_roots()
    dirs: list[Path] = []
    for root in roots:
        try:
            exists = root.exists()
        except OSError:
            continue
        if not exists:
            continue
        try:
            dirs.extend(root.glob("*/Local Storage/leveldb"))
        except OSError:
            continue
    return dirs


def token_candidates() -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []

    for env_name in ("AMSC_SESSION_ID", "AMSC_TOKEN"):
        token = os.environ.get(env_name, "").strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for leveldb_dir in chrome_leveldb_dirs():
        for path in leveldb_dir.iterdir():
            if not path.is_file() or path.suffix not in {".ldb", ".log", ""}:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if b"amsc.net.cn" not in blob and b"sessionId" not in blob and b"token" not in blob:
                continue
            for match in TOKEN_RE.finditer(blob):
                token = match.group(0).decode("ascii")
                if token not in seen:
                    seen.add(token)
                    tokens.append(token)

    return tokens


def query_params(token: str, count: int) -> dict[str, str]:
    return {
        "isCCCC": "cccc",
        "cccc": AIRPORT,
        "count": str(count),
        "tt": QUERY_TT,
        "token": token,
    }


def request_headers(token: str) -> dict[str, str]:
    return {
        "sessionId": token,
        "app": "AMS",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def fetch_with_urllib(token: str, count: int, timeout: int) -> dict[str, Any]:
    url = ENDPOINT + "?" + urllib.parse.urlencode(query_params(token, count))
    req = urllib.request.Request(url, headers=request_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def fetch_with_curl(token: str, count: int, timeout: int) -> dict[str, Any]:
    connect_timeout = max(1, min(int(timeout), 5))
    cmd = [
        "curl",
        "-sS",
        "-L",
        "--compressed",
        "--connect-timeout",
        str(connect_timeout),
        "--max-time",
        str(timeout),
        "--retry",
        "0",
        "-G",
        ENDPOINT,
        "-H",
        f"sessionId: {token}",
        "-H",
        "app: AMS",
        "-H",
        "Content-Type: application/json",
    ]
    for key, value in query_params(token, count).items():
        cmd.extend(["--data-urlencode", f"{key}={value}"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl exited with {result.returncode}")
    return json.loads(result.stdout)


def fetch_reports(token: str, count: int, timeout: int) -> dict[str, Any]:
    try:
        return fetch_with_urllib(token, count, timeout)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return fetch_with_curl(token, count, timeout)


def fetch_reports_with_transport(token: str, count: int, timeout: int) -> tuple[dict[str, Any], dict[str, str]]:
    """Fetch one poll and retain only genuinely observed transport milestones.

    The urllib response context is entered only after response headers arrive,
    so its timestamp is a usable response-start observation.  curl fallback
    deliberately returns no such timestamp rather than inventing one from the
    poll clock; those raw records remain archive-valid but cannot support a
    strict Tier-A TAF forecast proposal.
    """

    url = ENDPOINT + "?" + urllib.parse.urlencode(query_params(token, count))
    req = urllib.request.Request(url, headers=request_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_started_at = utc_now_iso()
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw), {
            "response_started_at": response_started_at,
            "response_headers_observed": True,
            "transport_mode": "urllib_observed_headers",
        }
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return fetch_with_curl(token, count, timeout), {
            "response_started_at": "",
            "response_headers_observed": False,
            "transport_mode": "curl_response_start_unobserved",
        }


def filtered_report_texts(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []

    reports: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        prefix = content.split(None, 1)[0].upper()
        if prefix in REPORT_TYPES:
            reports.append(content)
    return reports


def _parse_signed_temp(value: str) -> float | None:
    text = str(value or "").strip()
    if not text or text == "//":
        return None
    sign = -1 if text.startswith("M") else 1
    digits = text[1:] if sign < 0 else text
    try:
        return float(sign * int(digits))
    except ValueError:
        return None


def _round_float(value: float | None, ndigits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _metar_sections(content: str) -> tuple[str, str]:
    parts = METAR_TREND_START_RE.split(str(content or ""), maxsplit=1)
    current = parts[0].strip()
    trend = parts[1].strip() if len(parts) > 1 else ""
    return current, trend


def _metar_wind(content: str) -> dict[str, float | str | bool | None]:
    match = METAR_WIND_RE.search(content)
    if not match:
        return {
            "wind_dir_deg": None,
            "wind_speed_mps": None,
            "wind_gust_mps": None,
            "wind_variable": False,
            "wind_variable_from_deg": None,
            "wind_variable_to_deg": None,
        }
    unit = match.group("unit")
    factor = 0.514444 if unit == "KT" else 1.0
    direction = match.group("direction")
    speed = float(match.group("speed")) * factor
    gust = float(match.group("gust")) * factor if match.group("gust") else None
    variation = METAR_WIND_VARIATION_RE.search(content[match.end():])
    return {
        "wind_dir_deg": None if direction == "VRB" else float(direction),
        "wind_speed_mps": _round_float(speed, 3),
        "wind_gust_mps": _round_float(gust, 3),
        "wind_variable": bool(direction == "VRB" or variation),
        "wind_variable_from_deg": float(variation.group("from")) if variation else None,
        "wind_variable_to_deg": float(variation.group("to")) if variation else None,
    }


def _metar_visibility_m(content: str) -> float | None:
    if " CAVOK" in f" {content} ":
        return 9999.0
    wind_match = METAR_WIND_RE.search(content)
    search_from = wind_match.end() if wind_match else 0
    for match in re.finditer(r"\b(\d{4})\b", content[search_from:]):
        value = int(match.group(1))
        if 0 <= value <= 9999:
            return float(value)
    return None


def _metar_pressure_hpa(content: str) -> float | None:
    match = METAR_QNH_RE.search(content)
    if match:
        return float(match.group(1))
    match = METAR_ALTIMETER_RE.search(content)
    if not match:
        return None
    inches_hg = float(match.group(1)) / 100.0
    return _round_float(inches_hg * 33.8638866667, 3)


def _metar_cloud_tokens(content: str) -> list[str]:
    return [match.group(0) for match in METAR_CLOUD_RE.finditer(content)]


def _metar_weather_tokens(content: str) -> list[str]:
    return [match.group(0) for match in METAR_WEATHER_RE.finditer(content)]


def _cloud_cover_pct(tokens: list[str]) -> float | None:
    if not tokens:
        return None
    cover_values = {
        "CAVOK": 5.0,
        "NSC": 5.0,
        "SKC": 5.0,
        "CLR": 5.0,
        "FEW": 15.0,
        "SCT": 38.0,
        "BKN": 75.0,
        "OVC": 92.0,
        "VV": 95.0,
    }
    values: list[float] = []
    for token in tokens:
        key = token[:3].upper()
        values.append(float(cover_values.get(token.upper(), cover_values.get(key, 45.0))))
    return max(values) if values else None


def _low_cloud_cover_pct(tokens: list[str]) -> float | None:
    low_tokens: list[str] = []
    for token in tokens:
        if token.upper() in {"CAVOK", "NSC", "SKC", "CLR"}:
            low_tokens.append(token)
            continue
        height_match = re.search(r"(\d{3})", token)
        if height_match and int(height_match.group(1)) <= 30:
            low_tokens.append(token)
    return _cloud_cover_pct(low_tokens)


def _month_candidates(ref_utc: datetime) -> list[tuple[int, int]]:
    center = ref_utc.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
    months = {
        (center.year, center.month),
        ((center - timedelta(days=32)).year, (center - timedelta(days=32)).month),
        ((center + timedelta(days=32)).year, (center + timedelta(days=32)).month),
    }
    return sorted(months)


def _infer_report_time_utc(day: int, hour: int, minute: int, ref_utc: datetime) -> datetime | None:
    candidates: list[datetime] = []
    for year, month in _month_candidates(ref_utc):
        if day < 1 or day > calendar.monthrange(year, month)[1]:
            continue
        candidates.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs((item - ref_utc).total_seconds()))


def _iso_z(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(LOCAL_TZ).replace(microsecond=0).isoformat()


def _event_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(LOCAL_TZ).date().isoformat()


def _report_content(row: Mapping[str, Any]) -> str:
    return str(row.get("content") or "").strip()


def _amsc_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _build_metar_source_payload(
    row: dict[str, Any],
    *,
    fetched_at: str,
    duration_ms: float | None,
    valid_time_reference_at: str = "",
) -> dict[str, Any] | None:
    content = _report_content(row)
    report_type = content.split(None, 1)[0].upper() if content else ""
    if report_type not in {"METAR", "SPECI"}:
        return None
    match = OBSERVATION_TIME_RE.search(content)
    if not match:
        return None
    # A live poll uses its fetch time as the closest available calendar
    # reference.  Historical AMSC retrieval is different: it can return a
    # report from years ago while its download time is today, so its caller
    # must be able to bind the DDHHMMZ token to the queried UTC interval
    # without pretending that the report arrived today.
    reference_text = str(valid_time_reference_at or fetched_at)
    reference_dt = datetime.fromisoformat(reference_text.replace("Z", "+00:00"))
    valid_dt = _infer_report_time_utc(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        reference_dt,
    )
    current_content, trend_content = _metar_sections(content)
    temp_c = dewpoint_c = None
    temp_match = METAR_TEMP_RE.search(current_content)
    if temp_match:
        temp_c = _parse_signed_temp(temp_match.group(1))
        dewpoint_c = _parse_signed_temp(temp_match.group(2))
    wind = _metar_wind(current_content)
    cloud_tokens = _metar_cloud_tokens(current_content)
    weather_tokens = _metar_weather_tokens(current_content)
    trend_cloud_tokens = _metar_cloud_tokens(trend_content)
    trend_weather_tokens = _metar_weather_tokens(trend_content)
    return {
        "source": "amsc_metar",
        "source_id": "zuuu_metar_raw_amsc",
        "provider": "AMSC PacketRetrieval",
        "station_id": AIRPORT,
        "event_date": _event_date(valid_dt),
        "valid_time_utc": _iso_z(valid_dt),
        "valid_time_local": _local_iso(valid_dt),
        "temperature_c": temp_c,
        "dewpoint_c": dewpoint_c,
        "visibility_m": _metar_visibility_m(current_content),
        "cloud_text": " ".join(cloud_tokens),
        "clouds": " ".join(cloud_tokens),
        "cloud_cover_pct": _cloud_cover_pct(cloud_tokens),
        "low_cloud_cover_pct": _low_cloud_cover_pct(cloud_tokens),
        "weather_text": " ".join(weather_tokens),
        "metar_current_no_significant_weather": not bool(weather_tokens),
        "metar_trend_text": trend_content,
        "metar_trend_cloud_text": " ".join(trend_cloud_tokens),
        "metar_trend_weather_text": " ".join(trend_weather_tokens),
        "metar_trend_nosig": bool("NOSIG" in current_content and not trend_content),
        **wind,
        "pressure_hpa": _metar_pressure_hpa(current_content),
        "raw_ref": content,
        "raw_text": content,
        "report_type": report_type,
        # Captured immediately after the complete AMSC response is parsed.
        # The raw archive preserves the earliest value for a repeated physical
        # report; a re-fetch must never create a new T1.
        "first_seen_at": fetched_at,
        "as_of_eligible_at": fetched_at,
        "arrival_time_quality": "observed_first_seen",
        "airport_name": row.get("airportName") or "",
        "airport_en_name": row.get("airportEnName") or "",
        "transport": {
            "url": ENDPOINT,
            "request_completed_at": fetched_at,
            "duration_ms": duration_ms,
        },
    }


def _build_taf_source_payload(row: dict[str, Any], *, fetched_at: str, duration_ms: float | None) -> dict[str, Any] | None:
    content = _report_content(row)
    try:
        revision = parse_zuuu_taf_revision(content, first_seen_at=fetched_at)
    except ValueError:
        return None
    peak = max(revision["forecast_peaks"], key=lambda item: float(item["temperature_c"]))
    issue_dt = datetime.fromisoformat(str(revision["issue_time_utc"]).replace("Z", "+00:00"))
    peak_dt = datetime.fromisoformat(str(peak["valid_time_utc"]).replace("Z", "+00:00"))
    return {
        "source": "amsc_taf",
        "source_id": "zuuu_taf_forecast_amsc",
        "provider": "AMSC PacketRetrieval",
        "station_id": AIRPORT,
        "event_date": _event_date(peak_dt),
        "valid_time_utc": _iso_z(peak_dt),
        "valid_time_local": _local_iso(peak_dt),
        "forecast_peak_c": float(peak["temperature_c"]),
        "forecast_peak_valid_time": _iso_z(peak_dt),
        "raw_ref": content,
        "raw_text": content,
        "report_type": "TAF",
        "forecast_revision_kind": revision["forecast_revision_kind"],
        "taf_modifier": revision["taf_modifier"],
        "source_revision_id": revision["source_revision_id"],
        "issue_time_utc": _iso_z(issue_dt),
        "issue_time_local": _local_iso(issue_dt),
        "airport_name": row.get("airportName") or "",
        "airport_en_name": row.get("airportEnName") or "",
        "first_seen_at": fetched_at,
        "as_of_eligible_at": fetched_at,
        "arrival_time_quality": "observed_first_seen",
        "transport": {
            "url": ENDPOINT,
            "request_completed_at": fetched_at,
            "duration_ms": duration_ms,
        },
    }


def source_payloads_from_amsc_payload(
    payload: dict[str, Any],
    *,
    fetched_at: str,
    duration_ms: float | None = None,
    valid_time_reference_at: str = "",
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for row in _amsc_rows(payload):
        content = _report_content(row)
        if content.startswith(("METAR ", "SPECI ")):
            source = _build_metar_source_payload(
                row,
                fetched_at=fetched_at,
                duration_ms=duration_ms,
                valid_time_reference_at=valid_time_reference_at,
            )
        elif content.startswith("TAF "):
            source = _build_taf_source_payload(row, fetched_at=fetched_at, duration_ms=duration_ms)
        else:
            source = None
        if source:
            sources.append(source)
    return sources


def build_amsc_observation_bundle(
    payload: dict[str, Any],
    *,
    fetched_at: str,
    duration_ms: float | None = None,
    include_payload: bool = True,
    source_payloads: list[dict[str, Any]] | None = None,
    raw_history_archive: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sources = list(source_payloads) if source_payloads is not None else source_payloads_from_amsc_payload(
        payload,
        fetched_at=fetched_at,
        duration_ms=duration_ms,
    )
    if raw_history_archive:
        sources = attach_archive_metadata(sources, raw_history_archive)
    trace = build_source_acquisition_trace(
        sources=sources,
        station_id=AIRPORT,
        fetched_at=fetched_at,
        parser_version=PARSER_VERSION,
        include_payload=include_payload,
    )
    return {
        "schema": "amsc_zuuu_metar_taf_observation_bundle_v1",
        "read_only": True,
        "station_id": AIRPORT,
        "fetched_at": fetched_at,
        "source_payload_count": len(sources),
        "source_observations": trace["source_observations"],
        "source_observation_ids": trace["source_observation_ids"],
        "source_acquisition_trace": trace,
        "raw_history_archive": dict(raw_history_archive or {}),
        "guards": trace["guards"],
        "next_boundary": "weather_fact_snapshot_writer_dry_run_or_no_apply",
    }


def _parse_iso_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_date_matches(row: Mapping[str, Any], *, event_date: str) -> bool:
    expected = str(event_date or "").strip()
    if not expected:
        return True
    actual = str(row.get("event_date") or "").strip()
    return not actual or actual == expected


def _latest_observation(
    bundle: Mapping[str, Any],
    *,
    source_id: str,
    event_date: str = "",
) -> Mapping[str, Any]:
    observations = bundle.get("source_observations")
    if not isinstance(observations, list):
        return {}
    matches = [
        row
        for row in observations
        if isinstance(row, Mapping) and str(row.get("source_id") or "") == source_id
        and _event_date_matches(row, event_date=event_date)
    ]
    if not matches:
        return {}

    def key(row: Mapping[str, Any]) -> datetime:
        return _parse_iso_dt(str(row.get("valid_time") or "")) or datetime.min.replace(tzinfo=timezone.utc)

    return max(matches, key=key)


def _observations_for_source(bundle: Mapping[str, Any], *, source_id: str) -> list[Mapping[str, Any]]:
    observations = bundle.get("source_observations")
    if not isinstance(observations, list):
        return []
    matches = [
        row
        for row in observations
        if isinstance(row, Mapping) and str(row.get("source_id") or "") == source_id
    ]
    return sorted(matches, key=lambda row: _parse_iso_dt(str(row.get("valid_time") or "")) or datetime.min.replace(tzinfo=timezone.utc))


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _local_hour(value: str) -> float | None:
    parsed = _parse_iso_dt(value)
    if parsed is None:
        return None
    local = parsed.astimezone(LOCAL_TZ)
    return float(local.hour) + float(local.minute) / 60.0 + float(local.second) / 3600.0


def _row_value(row: Mapping[str, Any], *fields: str) -> Any:
    payload = _payload(row)
    for field in fields:
        if row.get(field) not in (None, ""):
            return row.get(field)
        if payload.get(field) not in (None, ""):
            return payload.get(field)
    return None


def _same_event_metar_points(bundle: Mapping[str, Any], *, event_date: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in _observations_for_source(bundle, source_id="zuuu_metar_raw_amsc"):
        if event_date and str(row.get("event_date") or "") != event_date:
            continue
        temp_c = _float_or_none(row.get("temperature_c"))
        if temp_c is None:
            continue
        points.append(
            {
                "at": str(row.get("valid_time") or ""),
                "valid_time": str(row.get("valid_time") or ""),
                "valid_time_local": str(row.get("valid_time_local") or ""),
                "fetched_at": str(row.get("fetched_at") or row.get("response_completed_at") or ""),
                "response_completed_at": str(row.get("response_completed_at") or row.get("fetched_at") or ""),
                "response_duration_ms": _float_or_none(row.get("response_duration_ms")),
                "temperature_c": temp_c,
                "dewpoint_c": _float_or_none(row.get("dewpoint_c")),
                "visibility_m": _float_or_none(_row_value(row, "visibility_m")),
                "cloud_cover_pct": _float_or_none(_row_value(row, "cloud_cover_pct")),
                "low_cloud_cover_pct": _float_or_none(_row_value(row, "low_cloud_cover_pct")),
                "cloud_text": str(_row_value(row, "cloud_text", "clouds") or ""),
                "weather_text": str(_row_value(row, "weather_text") or ""),
                "metar_current_no_significant_weather": bool(
                    _row_value(row, "metar_current_no_significant_weather")
                ),
                "metar_trend_text": str(_row_value(row, "metar_trend_text") or ""),
                "metar_trend_cloud_text": str(_row_value(row, "metar_trend_cloud_text") or ""),
                "metar_trend_weather_text": str(_row_value(row, "metar_trend_weather_text") or ""),
                "metar_trend_nosig": bool(_row_value(row, "metar_trend_nosig")),
                "wind_speed_mps": _float_or_none(_row_value(row, "wind_speed_mps")),
                "wind_dir_deg": _float_or_none(_row_value(row, "wind_dir_deg")),
                "wind_gust_mps": _float_or_none(_row_value(row, "wind_gust_mps")),
                "wind_variable": bool(_row_value(row, "wind_variable")),
                "wind_variable_from_deg": _float_or_none(_row_value(row, "wind_variable_from_deg")),
                "wind_variable_to_deg": _float_or_none(_row_value(row, "wind_variable_to_deg")),
                "pressure_hpa": _float_or_none(_row_value(row, "pressure_hpa")),
                "raw": str(row.get("raw_ref") or _row_value(row, "raw_text") or ""),
                "source_id": str(row.get("source_id") or ""),
                "source_observation_id": str(row.get("source_observation_id") or ""),
            }
        )
    return points


def _momentum_rates_from_points(points: list[dict[str, Any]]) -> dict[str, float]:
    if len(points) < 2:
        return {}
    parsed_points = [
        (
            _parse_iso_dt(str(point.get("valid_time") or "")),
            _float_or_none(point.get("temperature_c")),
        )
        for point in points
    ]
    parsed_points = [(dt, temp) for dt, temp in parsed_points if dt is not None and temp is not None]
    if len(parsed_points) < 2:
        return {}
    parsed_points.sort(key=lambda item: item[0])
    latest_dt, latest_temp = parsed_points[-1]
    rates: dict[str, float] = {}
    for label, hours in TEMPERATURE_MOMENTUM_WINDOWS_HOURS.items():
        target_seconds = float(hours) * 3600.0
        eligible = [
            (dt, temp)
            for dt, temp in parsed_points[:-1]
            if abs((latest_dt - dt).total_seconds() - target_seconds)
            <= AMSC_MOMENTUM_NODE_TOLERANCE_SECONDS
        ]
        if not eligible:
            continue
        prior_dt, prior_temp = min(
            eligible,
            key=lambda item: abs((latest_dt - item[0]).total_seconds() - target_seconds),
        )
        elapsed_hours = max((latest_dt - prior_dt).total_seconds() / 3600.0, 1e-6)
        delta_c = float(latest_temp) - float(prior_temp)
        if label == "30m" and abs(delta_c) <= METAR_ADJACENT_STEP_NOISE_C:
            delta_c = 0.0
        elif label == "30m":
            delta_c = math.copysign(abs(delta_c) - METAR_INTEGER_QUANTIZATION_NOISE_C, delta_c)
        rates[label] = round(delta_c / elapsed_hours, 6)
    return rates


def _rolling_temperature_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    parsed_points = [
        (point, _parse_iso_dt(str(point.get("valid_time") or "")), _float_or_none(point.get("temperature_c")))
        for point in points[-5:]
    ]
    parsed_points = [(point, dt, temp) for point, dt, temp in parsed_points if dt is not None and temp is not None]
    if len(parsed_points) < 3:
        return {
            "state": "insufficient_points",
            "point_count": len(parsed_points),
            "stable_rate_c_per_hour": None,
        }
    first_point, first_dt, first_temp = parsed_points[0]
    last_point, last_dt, last_temp = parsed_points[-1]
    temps = [float(temp) for _, _, temp in parsed_points]
    rolling_max = max(temps)
    total_delta = float(last_temp) - float(first_temp)
    duration_hours = max((last_dt - first_dt).total_seconds() / 3600.0, 1e-6)
    if abs(total_delta) <= METAR_ADJACENT_STEP_NOISE_C:
        adjusted_delta = 0.0
    else:
        adjusted_delta = (abs(total_delta) - METAR_INTEGER_QUANTIZATION_NOISE_C) * (1.0 if total_delta > 0 else -1.0)
    slope = adjusted_delta / duration_hours
    latest_drop = rolling_max - float(last_temp)
    if latest_drop <= METAR_PLATFORM_WITHIN_ROLLING_MAX_C:
        state = "platform"
    elif slope >= 0.35:
        state = "sustained_warming"
    elif slope >= 0.15:
        state = "weak_warming"
    elif slope <= -0.35:
        state = "cooling"
    else:
        state = "flat"
    return {
        "state": state,
        "point_count": len(parsed_points),
        "rolling_max_c": round(rolling_max, 6),
        "latest_drop_from_rolling_max_c": round(latest_drop, 6),
        "stable_rate_c_per_hour": round(slope, 6),
        "duration_hours": round(duration_hours, 6),
        "first_valid_time": str(first_point.get("valid_time") or ""),
        "latest_valid_time": str(last_point.get("valid_time") or ""),
    }


def _peak_window_features(taf: Mapping[str, Any]) -> dict[str, Any]:
    peak_local = str(taf.get("valid_time_local") or "").strip()
    peak_hour = _local_hour(peak_local)
    if peak_hour is None:
        return {}
    start = max(0.0, float(peak_hour) - PEAK_WINDOW_LEAD_HOURS)
    end = min(23.99, float(peak_hour) + PEAK_WINDOW_POST_HOURS)
    return {
        "expected_peak_hour_local": round(float(peak_hour), 3),
        "expected_peak_at_local": peak_local,
        "expected_peak_window_start_hour_local": round(start, 3),
        "expected_peak_window_end_hour_local": round(end, 3),
        "expected_peak_window_source": "zuuu_taf_forecast_amsc_TX_expanded_window",
        "expected_peak_window_basis": "taf_tx_time_expanded_not_single_point",
    }


def _default_slug_for_event_date(event_date: str) -> str:
    try:
        date_obj = datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return "highest-temperature-in-chengdu"
    month = calendar.month_name[date_obj.month].lower()
    return f"highest-temperature-in-chengdu-on-{month}-{date_obj.day}-{date_obj.year}"


def _default_event_date(bundle: Mapping[str, Any]) -> str:
    metar = _latest_observation(bundle, source_id="zuuu_metar_raw_amsc")
    event_date = str(metar.get("event_date") or "").strip()
    if event_date:
        return event_date
    fetched_dt = _parse_iso_dt(str(bundle.get("fetched_at") or ""))
    return _event_date(fetched_dt)


def _weather_likelihood_features_from_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    metar = _latest_observation(bundle, source_id="zuuu_metar_raw_amsc")
    metar_payload = metar.get("payload") if isinstance(metar.get("payload"), Mapping) else {}
    event_date = str(metar.get("event_date") or "").strip()
    taf = _latest_observation(bundle, source_id="zuuu_taf_forecast_amsc", event_date=event_date)
    taf_payload = taf.get("payload") if isinstance(taf.get("payload"), Mapping) else {}
    points = _same_event_metar_points(bundle, event_date=event_date)
    max_point = max(points, key=lambda item: float(item.get("temperature_c") or -999.0)) if points else {}
    rates = _momentum_rates_from_points(points)
    rolling = _rolling_temperature_summary(points)
    latest_temp_c = _float_or_none(metar.get("temperature_c"))
    latest_dewpoint_c = _float_or_none(metar.get("dewpoint_c"))
    latest_raw = str(metar.get("raw_ref") or metar_payload.get("raw_text") or "")
    today_max_c = _float_or_none(max_point.get("temperature_c")) if max_point else latest_temp_c
    metar_transport = metar_payload.get("transport") if isinstance(metar_payload.get("transport"), Mapping) else {}
    latest_fetched_at = str(
        metar.get("fetched_at")
        or metar.get("response_completed_at")
        or bundle.get("fetched_at")
        or ""
    )
    latest_response_completed_at = str(
        metar.get("response_completed_at")
        or metar.get("fetched_at")
        or bundle.get("fetched_at")
        or ""
    )
    latest_response_duration_ms = _float_or_none(
        metar.get("response_duration_ms")
        if metar.get("response_duration_ms") is not None
        else metar_transport.get("duration_ms")
    )
    today_max_fetched_at = str(
        max_point.get("fetched_at")
        or max_point.get("response_completed_at")
        or latest_fetched_at
    )
    features: dict[str, Any] = {
        "schema": "amsc_zuuu_metar_taf_features_v1",
        "station_id": AIRPORT,
        "source": "amsc_packet_retrieval",
        "source_update_cadence_sec": 1800,
        "metar_observation_source": "zuuu_metar_raw_amsc" if metar else "",
        "observed_temp_source": "zuuu_metar_raw_amsc" if latest_temp_c is not None else "",
        "observed_today_max_source": "zuuu_metar_raw_amsc" if today_max_c is not None else "",
        "temperature_momentum_source": "zuuu_metar_raw_amsc" if rates or rolling.get("stable_rate_c_per_hour") is not None else "",
        "temperature_path_source": "zuuu_metar_raw_amsc" if points else "",
        "metar_temperature_c": latest_temp_c,
        "observed_temp_c": latest_temp_c,
        "observed_today_max_c": today_max_c,
        "dewpoint_c": latest_dewpoint_c,
        "visibility_m": _float_or_none(_row_value(metar, "visibility_m")),
        "cloud_cover_pct": _float_or_none(_row_value(metar, "cloud_cover_pct")),
        "low_cloud_cover_pct": _float_or_none(_row_value(metar, "low_cloud_cover_pct")),
        "cloud_text": str(_row_value(metar, "cloud_text", "clouds") or ""),
        "weather_text": str(_row_value(metar, "weather_text") or ""),
        "metar_current_no_significant_weather": bool(
            _row_value(metar, "metar_current_no_significant_weather")
        ),
        "wind_speed_mps": _float_or_none(_row_value(metar, "wind_speed_mps")),
        "wind_dir_deg": _float_or_none(_row_value(metar, "wind_dir_deg")),
        "wind_gust_mps": _float_or_none(_row_value(metar, "wind_gust_mps")),
        "wind_variable": bool(_row_value(metar, "wind_variable")),
        "wind_variable_from_deg": _float_or_none(_row_value(metar, "wind_variable_from_deg")),
        "wind_variable_to_deg": _float_or_none(_row_value(metar, "wind_variable_to_deg")),
        "pressure_hpa": _float_or_none(_row_value(metar, "pressure_hpa")),
        "metar_trend_text": str(_row_value(metar, "metar_trend_text") or ""),
        "metar_trend_cloud_text": str(_row_value(metar, "metar_trend_cloud_text") or ""),
        "metar_trend_weather_text": str(_row_value(metar, "metar_trend_weather_text") or ""),
        "metar_trend_nosig": bool(_row_value(metar, "metar_trend_nosig")),
        "zuuu_metar_raw_amsc_latest_at": metar.get("valid_time") or "",
        "zuuu_metar_raw_amsc_latest_local": metar.get("valid_time_local") or "",
        "zuuu_metar_raw_amsc_fetched_at": latest_fetched_at,
        "zuuu_metar_raw_amsc_latest_fetched_at": latest_fetched_at,
        "zuuu_metar_raw_amsc_response_completed_at": latest_response_completed_at,
        "zuuu_metar_raw_amsc_fetch_duration_ms": latest_response_duration_ms,
        "zuuu_metar_raw_amsc_latest_temp_c": latest_temp_c,
        "zuuu_metar_raw_amsc_latest_dewpoint_c": latest_dewpoint_c,
        "zuuu_metar_raw_amsc_latest_visibility_m": _float_or_none(_row_value(metar, "visibility_m")),
        "zuuu_metar_raw_amsc_latest_cloud_cover_pct": _float_or_none(_row_value(metar, "cloud_cover_pct")),
        "zuuu_metar_raw_amsc_latest_low_cloud_cover_pct": _float_or_none(_row_value(metar, "low_cloud_cover_pct")),
        "zuuu_metar_raw_amsc_latest_cloud_text": str(_row_value(metar, "cloud_text", "clouds") or ""),
        "zuuu_metar_raw_amsc_latest_weather_text": str(_row_value(metar, "weather_text") or ""),
        "zuuu_metar_raw_amsc_latest_no_significant_weather": bool(
            _row_value(metar, "metar_current_no_significant_weather")
        ),
        "zuuu_metar_raw_amsc_latest_wind_dir_deg": _float_or_none(_row_value(metar, "wind_dir_deg")),
        "zuuu_metar_raw_amsc_latest_wind_speed_mps": _float_or_none(_row_value(metar, "wind_speed_mps")),
        "zuuu_metar_raw_amsc_latest_wind_gust_mps": _float_or_none(_row_value(metar, "wind_gust_mps")),
        "zuuu_metar_raw_amsc_latest_wind_variable": bool(_row_value(metar, "wind_variable")),
        "zuuu_metar_raw_amsc_latest_wind_variable_from_deg": _float_or_none(_row_value(metar, "wind_variable_from_deg")),
        "zuuu_metar_raw_amsc_latest_wind_variable_to_deg": _float_or_none(_row_value(metar, "wind_variable_to_deg")),
        "zuuu_metar_raw_amsc_latest_pressure_hpa": _float_or_none(_row_value(metar, "pressure_hpa")),
        "zuuu_metar_raw_amsc_latest_trend_text": str(_row_value(metar, "metar_trend_text") or ""),
        "zuuu_metar_raw_amsc_latest_trend_cloud_text": str(_row_value(metar, "metar_trend_cloud_text") or ""),
        "zuuu_metar_raw_amsc_latest_trend_weather_text": str(_row_value(metar, "metar_trend_weather_text") or ""),
        "zuuu_metar_raw_amsc_latest_trend_nosig": bool(_row_value(metar, "metar_trend_nosig")),
        "zuuu_metar_raw_amsc_latest_raw": latest_raw,
        "zuuu_metar_raw_amsc_today_max_c": today_max_c,
        "zuuu_metar_raw_amsc_today_max_at": max_point.get("valid_time") or metar.get("valid_time") or "",
        "zuuu_metar_raw_amsc_today_max_local": max_point.get("valid_time_local") or metar.get("valid_time_local") or "",
        "zuuu_metar_raw_amsc_today_max_fetched_at": today_max_fetched_at,
        "zuuu_metar_raw_amsc_today_max_raw": max_point.get("raw") or latest_raw,
        "zuuu_metar_raw_amsc_today_temperature_points": points,
        "zuuu_metar_raw_amsc_temperature_observations": points,
        "zuuu_metar_raw_amsc_temperature_point_count": len(points),
        "zuuu_metar_raw_amsc_source_observation_id": metar.get("source_observation_id") or "",
        "zuuu_metar_raw_amsc_transport": dict(metar_payload.get("transport") or {}),
        "taf_forecast_peak_c": taf.get("forecast_peak_c"),
        "taf_forecast_peak_at": taf.get("valid_time") or "",
        "taf_forecast_peak_local": taf.get("valid_time_local") or "",
        "taf_forecast_peak_raw": taf.get("raw_ref") or "",
        "taf_forecast_peak_source": taf.get("source_id") or "",
        "taf_forecast_peak_source_observation_id": taf.get("source_observation_id") or "",
        "taf_forecast_peak_transport": dict(taf_payload.get("transport") or {}),
    }
    for label, rate in rates.items():
        features[f"zuuu_metar_raw_amsc_temperature_momentum_{label}_c_per_hour"] = rate
        features[f"temperature_momentum_{label}_c_per_hour"] = rate
    if rates:
        available_horizons = ",".join(rates)
        features["zuuu_metar_raw_amsc_temperature_momentum_available_horizons"] = available_horizons
        features["temperature_momentum_available_horizons"] = available_horizons
    stable_rate = _float_or_none(rolling.get("stable_rate_c_per_hour"))
    if stable_rate is not None:
        features["temperature_trajectory_stable_rate_c_per_hour"] = stable_rate
        features["temperature_momentum_effective_c_per_hour"] = stable_rate
        features["zuuu_metar_raw_amsc_temperature_trajectory_stable_rate_c_per_hour"] = stable_rate
    if rolling.get("state"):
        features["temperature_trajectory_realization_state"] = rolling.get("state")
        features["zuuu_metar_raw_amsc_temperature_trajectory_realization_state"] = rolling.get("state")
    features["zuuu_metar_raw_amsc_rolling_temperature_summary"] = rolling
    features.update(_peak_window_features(taf))
    return features


def build_amsc_fact_snapshot_write_plan(
    bundle: Mapping[str, Any],
    *,
    slug: str = "",
    event_date: str = "",
    run_date: str = "",
    fact_as_of: str = "",
    include_payload: bool = True,
    enable_apply_contract: bool = False,
    write_latest: bool = False,
) -> dict[str, Any]:
    event_date_key = str(event_date or _default_event_date(bundle)).strip()
    run_date_key = str(run_date or event_date_key).strip()
    slug_key = str(slug or _default_slug_for_event_date(event_date_key)).strip()
    fact_as_of_text = str(fact_as_of or bundle.get("fetched_at") or utc_now_iso()).strip()
    requested_write_latest = bool(write_latest)
    effective_write_latest = False
    trace = bundle.get("source_acquisition_trace") if isinstance(bundle.get("source_acquisition_trace"), Mapping) else {}
    raw_archive = bundle.get("raw_history_archive") if isinstance(bundle.get("raw_history_archive"), Mapping) else {}
    raw_archive_summary = {
        key: raw_archive.get(key)
        for key in (
            "schema",
            "manifest_ref",
            "manifest_sha256",
            "objects_written",
            "objects_reused",
            "raw_object_count",
            "coverage_manifest_refs",
            "daily_coverage_state",
            "guards",
        )
        if raw_archive.get(key) not in (None, "")
    }
    incoming_payload = {
        "schema": "amsc_zuuu_metar_taf_fact_payload_v1",
        "slug": slug_key,
        "event_date": event_date_key,
        "run_date": run_date_key,
        "fact_as_of": fact_as_of_text,
        "station_id": AIRPORT,
        "source": "amsc_packet_retrieval",
        "source_update_cadence_sec": 1800,
        "weather_likelihood_features": _weather_likelihood_features_from_bundle(bundle),
        "source_observations": bundle.get("source_observations") or [],
        "source_observation_ids": bundle.get("source_observation_ids") or [],
        "source_acquisition_trace": dict(trace),
        "amsc_raw_history_archive": raw_archive_summary,
        "amsc_observation_bundle_schema": bundle.get("schema") or "",
        "source_acquisition_latest_repoint_allowed": False,
        "source_acquisition_latest_repoint_block_reason": AMSC_LATEST_REPOINT_BLOCK_REASON,
    }
    plan = build_fact_snapshot_write_plan(
        slug=slug_key,
        event_date=event_date_key,
        run_date=run_date_key,
        fact_as_of=fact_as_of_text,
        incoming_payload=incoming_payload,
        source_acquisition_trace=trace,
        composer_version=PARSER_VERSION,
        write_reason="amsc_zuuu_metar_taf_observation_bundle_dry_run",
        include_payload=include_payload,
        enable_apply_contract=enable_apply_contract,
        write_latest=effective_write_latest,
    )
    plan["caller"] = {
        "module": "scripts/fetch_zuuu_metar_taf.py",
        "function": "build_amsc_fact_snapshot_write_plan",
        "lane": "amsc_zuuu_metar_taf_read_only",
        "source_update_cadence_sec": 1800,
        "apply_contract_enabled": bool(enable_apply_contract),
        "write_latest_requested": requested_write_latest,
        "write_latest_effective": effective_write_latest,
        "write_latest_block_reason": AMSC_LATEST_REPOINT_BLOCK_REASON if requested_write_latest else "",
    }
    plan["source_acquisition_latest_repoint_policy"] = {
        "requested": requested_write_latest,
        "effective": effective_write_latest,
        "block_reason": AMSC_LATEST_REPOINT_BLOCK_REASON if requested_write_latest else "",
        "boundary": "source_acquisition_may_append_history_but_must_not_repoint_posterior_ssot_latest",
    }
    plan["guards"] = {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "places_orders": False,
        "historical_backfill": False,
        "restarts_launchd": False,
        "executes_sql": False,
    }
    return plan


def _write_json_atomic(path: str, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)


def _emit_json(payload: Mapping[str, Any], output_json: str = "") -> None:
    if output_json:
        _write_json_atomic(output_json, payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch only ZUUU METAR/TAF reports from AMSC and print report text only."
    )
    parser.add_argument("--count", type=int, default=48, help="latest copies per report code, default: 48")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds, default: 20")
    parser.add_argument(
        "--observations-json",
        action="store_true",
        help="print read-only SourceObservation bundle JSON instead of report text",
    )
    parser.add_argument(
        "--write-plan-json",
        action="store_true",
        help="print weather_fact_snapshot_writer dry-run write plan JSON; never applies it",
    )
    parser.add_argument(
        "--enable-apply-contract",
        action="store_true",
        help="mark the write plan apply-supported; still does not execute SQL or apply the plan",
    )
    parser.add_argument(
        "--write-latest",
        action="store_true",
        help="allow the plan to include latest repoint as apply-ready when policy permits; never applies it",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="write --observations-json or --write-plan-json output atomically to this path instead of stdout",
    )
    parser.add_argument(
        "--archive-dir",
        default="",
        help="append-only local raw-report archive directory; never writes archive.db or facts",
    )
    parser.add_argument(
        "--defer-daily-coverage",
        action="store_true",
        help="persist raw objects/manifest now and defer full-archive daily coverage until after probability T3",
    )
    parser.add_argument("--slug", default="", help="override event slug for --write-plan-json")
    parser.add_argument("--event-date", default="", help="override event date YYYY-MM-DD for --write-plan-json")
    parser.add_argument("--run-date", default="", help="override run date YYYY-MM-DD for --write-plan-json")
    parser.add_argument("--fact-as-of", default="", help="override fact_as_of timestamp for --write-plan-json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.count <= 0:
        print("count must be positive", file=sys.stderr)
        return 2

    tokens = token_candidates()
    if not tokens:
        print("AMSC session not found; login in Chrome or set AMSC_SESSION_ID.", file=sys.stderr)
        return 2

    last_error: str | None = None
    hard_timeout_seconds = max(int(args.timeout) + 3, 5)
    for token in tokens:
        started = time.monotonic()
        poll_started_at = utc_now_iso()
        try:
            with fetch_hard_timeout(hard_timeout_seconds):
                payload, transport_timing = fetch_reports_with_transport(token, args.count, args.timeout)
        except Exception as exc:  # Keep token probing read-only and continue to older Chrome records.
            last_error = str(exc)
            continue
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)
        fetched_at = utc_now_iso()

        if payload.get("code") != 200:
            last_error = str(payload.get("errMsg") or payload.get("msg") or payload)
            continue

        sources = source_payloads_from_amsc_payload(
            payload,
            fetched_at=fetched_at,
            duration_ms=duration_ms,
        )
        raw_history_archive: dict[str, Any] = {}
        if args.archive_dir:
            raw_history_archive = archive_amsc_poll(
                archive_dir=args.archive_dir,
                source_payloads=sources,
                poll_started_at=poll_started_at,
                response_completed_at=fetched_at,
                response_started_at=str(transport_timing.get("response_started_at") or ""),
                response_headers_observed=transport_timing.get("response_headers_observed") is True,
                response_status=payload.get("code"),
                request_metadata=query_params(token, args.count),
                fetcher_version=PARSER_VERSION,
                materialize_daily_coverage=not args.defer_daily_coverage,
            )

        if args.write_plan_json:
            bundle = build_amsc_observation_bundle(
                payload,
                fetched_at=fetched_at,
                duration_ms=duration_ms,
                source_payloads=sources,
                raw_history_archive=raw_history_archive,
            )
            plan = build_amsc_fact_snapshot_write_plan(
                bundle,
                slug=args.slug,
                event_date=args.event_date,
                run_date=args.run_date,
                fact_as_of=args.fact_as_of,
                include_payload=True,
                enable_apply_contract=args.enable_apply_contract,
                write_latest=args.write_latest,
            )
            _emit_json(plan, args.output_json)
            return 0

        if args.observations_json:
            bundle = build_amsc_observation_bundle(
                payload,
                fetched_at=fetched_at,
                duration_ms=duration_ms,
                source_payloads=sources,
                raw_history_archive=raw_history_archive,
            )
            _emit_json(bundle, args.output_json)
            return 0

        for report in filtered_report_texts(payload):
            print(report)
        return 0

    print(f"AMSC fetch failed: {last_error or 'no valid session'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
