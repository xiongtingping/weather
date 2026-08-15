"""Display-only official AWC TAF adapter for non-China copy-follow stations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import ssl
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from amsc_taf_revision_runtime import parse_taf_revision
from reported_trajectory_taf_sources import fetch_amsc_taf_reports


AWC_TAF_SOURCE_ID = "awc_taf_snapshots"
AMSC_TAF_SOURCE_ID = "amsc_taf_snapshots"
AWC_TAF_HOST = "aviationweather.gov"

# Used only when the caller does not provide the settlement timezone.  The
# production registry normally supplies the explicit timezone from the
# station's market metadata.
STATION_TIMEZONES = {
    "KAUS": "America/Chicago",
    "KORD": "America/Chicago",
    "EFHK": "Europe/Helsinki",
    "EGLC": "Europe/London",
    "LIMC": "Europe/Rome",
    "EDDM": "Europe/Berlin",
    "EPWA": "Europe/Warsaw",
    "SAEZ": "America/Argentina/Buenos_Aires",
    "RKSI": "Asia/Seoul",
    "ZBAA": "Asia/Shanghai",
    "ZGGG": "Asia/Shanghai",
    "ZGSZ": "Asia/Shanghai",
    "ZSPD": "Asia/Shanghai",
    "ZUCK": "Asia/Shanghai",
}


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
    # The local runtime can expose a TLS-intercepting proxy whose certificate
    # is not in the system trust store.  Prefer a direct verified connection;
    # if that environment still presents the local certificate, use the same
    # constrained direct official-host transport as the METAR source lane.
    direct = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    try:
        return direct.open(request, timeout=timeout)
    except ssl.SSLCertVerificationError:
        return build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl._create_unverified_context()),
        ).open(request, timeout=timeout)


def _safe_api_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or parsed.hostname != AWC_TAF_HOST:
        return ""
    return urlunsplit(("https", AWC_TAF_HOST, parsed.path or "/api/data/taf", "", ""))


def _station_timezone(station_id: str, timezone_name: str | None) -> str:
    return str(timezone_name or STATION_TIMEZONES.get(str(station_id or "").upper()) or "UTC")


def _unavailable(
    source_id: str,
    reason: str,
    *,
    request_url: str = "",
    issue_time_utc: str = "",
    raw_taf: str = "",
    parsed_peaks: Any = None,
    requested_event_date: str = "",
) -> dict[str, Any]:
    result = {
        "source_id": source_id,
        "state": "unavailable",
        "reason": reason,
        "peak_c": None,
        "rows": [],
    }
    if request_url:
        result["request_url"] = request_url
    if issue_time_utc:
        result["issue_time_utc"] = issue_time_utc
    # A TX that is present in the report but attributed to another local day
    # is indistinguishable, after the fact, from a report that carried no TX
    # at all -- both land as tx_available=0 with nothing kept.  Measured
    # 2026-08-15: ZUUU had TAFs for 2026-08-16 with an empty tx_c and no way
    # to tell which of the two had happened, because the raw report is
    # hashed but never stored.  When the peak filter is what refused, keep
    # the report and what the parser made of it.
    if raw_taf:
        result["raw_taf"] = raw_taf
    if parsed_peaks is not None:
        result["parsed_forecast_peaks"] = parsed_peaks
    if requested_event_date:
        result["requested_event_date"] = requested_event_date
    return result


def fetch_awc_taf_peak(
    *,
    station_id: str,
    event_date: str,
    api_url: str = "https://aviationweather.gov/api/data/taf",
    timeout_sec: float = 4.0,
    timezone_name: str | None = None,
    opener: Callable[..., Any] = _default_opener,
) -> dict[str, Any]:
    """Fetch the explicit TX peak from one current official AWC TAF."""

    station = str(station_id or "").strip().upper()
    day = str(event_date or "").strip()[:10]
    if not station or len(day) != 10:
        return _unavailable(AWC_TAF_SOURCE_ID, "identity_invalid")
    safe_url = _safe_api_url(api_url)
    if not safe_url:
        return _unavailable(AWC_TAF_SOURCE_ID, "api_host_not_allowlisted")
    request_url = f"{safe_url}?{urlencode({'ids': station, 'format': 'json'})}"
    request = Request(request_url, headers={"Accept": "application/json", "User-Agent": "weather-copy-follow-awc-taf/1"})
    try:
        with opener(request, timeout=float(timeout_sec)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return _unavailable(AWC_TAF_SOURCE_ID, f"request_failed:{type(exc).__name__}", request_url=request_url)
    reports = payload if isinstance(payload, list) else []
    report = next((item for item in reports if isinstance(item, Mapping) and str(item.get("icaoId") or "").upper() == station), None)
    raw_taf = str((report or {}).get("rawTAF") or "").strip()
    if not raw_taf:
        return _unavailable(AWC_TAF_SOURCE_ID, "taf_report_missing", request_url=request_url)
    issue_time = str((report or {}).get("issueTime") or "").strip() or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        parsed = parse_taf_revision(
            raw_taf,
            first_seen_at=issue_time,
            station_id=station,
            source_id="awc_taf_forecast",
            local_timezone=_station_timezone(station, timezone_name),
        )
    except ValueError as exc:
        # The shared parser intentionally rejects TAFs without an explicit TX.
        # At this adapter boundary that is a missing forecast value, not a
        # malformed report; keep genuine parser failures distinguishable.
        if str(exc) == "amsc_taf_tx_missing":
            return _unavailable(AWC_TAF_SOURCE_ID, "tx_peak_missing", request_url=request_url, issue_time_utc=issue_time)
        return _unavailable(AWC_TAF_SOURCE_ID, f"taf_parse_failed:{type(exc).__name__}", request_url=request_url)
    except TypeError as exc:
        return _unavailable(AWC_TAF_SOURCE_ID, f"taf_parse_failed:{type(exc).__name__}", request_url=request_url)
    peaks = [item for item in parsed.get("forecast_peaks") or [] if isinstance(item, Mapping) and str(item.get("event_date_local") or "") == day and _finite(item.get("temperature_c")) is not None]
    if not peaks:
        return _unavailable(
            AWC_TAF_SOURCE_ID,
            "tx_peak_missing",
            request_url=request_url,
            issue_time_utc=issue_time,
            raw_taf=raw_taf,
            parsed_peaks=list(parsed.get("forecast_peaks") or []),
            requested_event_date=day,
        )
    peak = max(peaks, key=lambda item: float(item["temperature_c"]))
    row = {
        "station_id": station,
        "event_date": day,
        "peak_c": float(peak["temperature_c"]),
        "issue_time_utc": str(parsed.get("issue_time_utc") or issue_time),
        "source_revision_id": str(parsed.get("source_revision_id") or ""),
        "peak_valid_time_utc": str(peak.get("valid_time_utc") or ""),
        "request_url": request_url,
    }
    return {"source_id": AWC_TAF_SOURCE_ID, "state": "available", "peak_c": row["peak_c"], "rows": [row], "request_url": request_url, "display_only": True}


def fetch_amsc_taf_peak(
    *,
    station_id: str,
    event_date: str,
    timezone_name: str | None = None,
    count: int = 12,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    """Fetch one official AMSC TAF peak as display-only supplemental evidence."""

    station = str(station_id or "").strip().upper()
    day = str(event_date or "").strip()[:10]
    if not station or len(day) != 10:
        return _unavailable(AMSC_TAF_SOURCE_ID, "identity_invalid")
    try:
        poll = fetch_amsc_taf_reports([station], count=int(count), timeout=float(timeout_sec))
    except Exception as exc:
        return _unavailable(AMSC_TAF_SOURCE_ID, f"request_failed:{type(exc).__name__}")

    parsed_rows: list[dict[str, Any]] = []
    for report in poll.get("reports") or []:
        if not isinstance(report, Mapping):
            continue
        raw_taf = str(report.get("raw_text") or "").strip()
        if not raw_taf:
            continue
        issue_time = str(report.get("source_report_time") or "").strip()
        if issue_time and "T" not in issue_time:
            issue_time = issue_time.replace(" ", "T") + "Z"
        if not issue_time:
            issue_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            parsed = parse_taf_revision(
                raw_taf,
                first_seen_at=issue_time,
                station_id=station,
                source_id="amsc_taf_forecast",
                local_timezone=_station_timezone(station, timezone_name),
            )
        except ValueError as exc:
            if str(exc) == "amsc_taf_tx_missing":
                continue
            continue
        peaks = [
            item for item in parsed.get("forecast_peaks") or []
            if isinstance(item, Mapping)
            and str(item.get("event_date_local") or "") == day
            and _finite(item.get("temperature_c")) is not None
        ]
        if not peaks:
            continue
        peak = max(peaks, key=lambda item: float(item["temperature_c"]))
        parsed_rows.append({
            "station_id": station,
            "event_date": day,
            "peak_c": float(peak["temperature_c"]),
            "issue_time_utc": str(parsed.get("issue_time_utc") or issue_time),
            "source_revision_id": str(parsed.get("source_revision_id") or ""),
            "peak_valid_time_utc": str(peak.get("valid_time_utc") or ""),
            "source_report_time": str(report.get("source_report_time") or ""),
        })
    if not parsed_rows:
        return _unavailable(AMSC_TAF_SOURCE_ID, "tx_peak_missing")
    row = max(parsed_rows, key=lambda item: item.get("issue_time_utc") or "")
    return {
        "source_id": AMSC_TAF_SOURCE_ID,
        "state": "available",
        "peak_c": row["peak_c"],
        "rows": [row],
        "display_only": True,
        "source_lane": "amsc_taf",
        "transport": poll.get("transport") or "official_amsc_taf_batch_cccc",
    }


__all__ = ["AMSC_TAF_SOURCE_ID", "AWC_TAF_SOURCE_ID", "fetch_amsc_taf_peak", "fetch_awc_taf_peak"]
