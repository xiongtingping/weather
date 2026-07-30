"""Bounded metadata probes for official historical forecast archives.

This module never downloads a GRIB payload.  It only records provider HTTP
metadata (or a small directory index for ICON) so that historical-source
availability cannot be confused with a calibration-ready arrival receipt.
The resulting audit is target research evidence and has no production
database, probability, market, or gateway side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any, Callable, Iterable, Mapping
from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_official_historical_archive_probe_v1"
MAX_INDEX_BYTES = 128 * 1024
MAX_LISTING_ENTRIES = 64
ALLOWED_HOSTS = {
    "ecmwf_ifs025_official": {"data.ecmwf.int"},
    "gfs_0p25_official": {"noaa-gfs-bdp-pds.s3.amazonaws.com"},
    "icon_global_official": {"opendata.dwd.de"},
}
SUPPORTED_RUN_CYCLES = ("00", "06", "12", "18")
# Probe a small, representative set of native ICON lead files.  This is
# deliberately metadata-only: the result distinguishes a missing date
# directory from a rolling current-cycle directory that still retains old
# dated filenames, without downloading any GRIB payload.
ICON_EXACT_PROBE_FORECAST_HOURS = (0, 6, 12, 24, 48)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_token(value: str) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise ContractError("archive_probe_date_invalid") from exc


def _cycle_token(value: str) -> str:
    cycle = str(value or "").strip()
    if cycle not in SUPPORTED_RUN_CYCLES:
        raise ContractError("archive_probe_cycle_invalid")
    return cycle


def _safe_url(url: str, provider_id: str) -> str:
    safe = str(url or "").strip()
    match = re.match(r"^https://([^/?#:]+)(?:[/?#]|$)", safe, flags=re.IGNORECASE)
    if match is None or match.group(1).lower() not in ALLOWED_HOSTS[provider_id]:
        raise ContractError("archive_probe_url_not_allowlisted")
    sensitive = {"api_key", "apikey", "key", "token", "access_token", "signature", "password", "secret"}
    query = safe.split("?", 1)[1].split("#", 1)[0] if "?" in safe else ""
    if any(part.split("=", 1)[0].lower() in sensitive for part in query.split("&") if part):
        raise ContractError("archive_probe_url_contains_credential")
    return safe


def _http_metadata(response: Any) -> dict[str, Any]:
    headers = getattr(response, "headers", {}) or {}
    status = int(getattr(response, "status", 200) or 200)
    content_length_raw = str(headers.get("Content-Length") or "").strip()
    content_length: int | None = None
    if content_length_raw:
        try:
            content_length = int(content_length_raw)
        except ValueError:
            content_length = None
    last_modified = str(headers.get("Last-Modified") or "").strip()
    publication_at = ""
    if last_modified:
        try:
            parsed = parsedate_to_datetime(last_modified)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone_required")
            publication_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            publication_at = "invalid"
    return {
        "status": status,
        "content_type": str(headers.get("Content-Type") or "").strip(),
        "content_length_bytes": content_length,
        "provider_last_modified": last_modified,
        "provider_publication_at_utc": publication_at,
    }


def _request_metadata(
    url: str,
    *,
    provider_id: str,
    opener: Callable[..., Any],
    timeout_sec: float,
    method: str = "HEAD",
    read_listing: bool = False,
) -> dict[str, Any]:
    safe = _safe_url(url, provider_id)
    result: dict[str, Any] = {"url": safe, "method": method, "status": "ERROR"}
    try:
        with opener(safe, method=method, timeout_sec=timeout_sec) as response:
            result.update(_http_metadata(response))
            result["status"] = "AVAILABLE" if int(result["status"]) == 200 else "UNAVAILABLE"
            if read_listing:
                body = bytes(response.read(MAX_INDEX_BYTES + 1) or b"")
                if len(body) > MAX_INDEX_BYTES:
                    result.update({"status": "BLOCKED", "reason": "archive_probe_listing_too_large"})
                else:
                    text = body.decode("utf-8", errors="replace")
                    hrefs = re.findall(r'href="([^"]+)"', text)[:MAX_LISTING_ENTRIES]
                    result["listing_entries"] = hrefs
                    result["listing_entry_count"] = len(hrefs)
    except Exception as exc:  # pragma: no cover - provider transport boundary
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            result.update({"status": "UNAVAILABLE", "http_status": code, "reason": "http_error"})
            return result
        result.update({"status": "ERROR", "reason": type(exc).__name__})
    return result


def _icon_t2m_url(*, run_date_token: str, cycle: str, forecast_hour: int) -> str:
    """Build one date-stamped ICON t2m object in the official cycle path."""

    if len(run_date_token) != 8 or not run_date_token.isdigit():
        raise ContractError("archive_probe_icon_date_token_invalid")
    cycle = _cycle_token(cycle)
    lead = int(forecast_hour)
    if lead < 0 or lead > 240:
        raise ContractError("archive_probe_icon_forecast_hour_invalid")
    filename = (
        f"icon_global_icosahedral_single-level_{run_date_token}{cycle}_"
        f"{lead:03d}_T_2M.grib2.bz2"
    )
    return f"https://opendata.dwd.de/weather/nwp/icon/grib/{cycle}/t_2m/{filename}"


def probe_official_archives(
    run_dates: Iterable[str],
    *,
    opener: Callable[..., Any],
    run_cycles: Iterable[str] = ("00",),
    timeout_sec: float = 15.0,
    probed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Probe date-addressable official metadata without retaining payloads."""

    dates = tuple(dict.fromkeys(_date_token(value) for value in run_dates))
    if not dates:
        raise ContractError("archive_probe_dates_empty")
    cycles = tuple(dict.fromkeys(_cycle_token(value) for value in run_cycles))
    if not cycles:
        raise ContractError("archive_probe_cycles_empty")
    rows: list[dict[str, Any]] = []
    for token in dates:
        for cycle in cycles:
            ecmwf = f"https://data.ecmwf.int/forecasts/{token}/{cycle}z/ifs/0p25/oper/{token}{cycle}0000-18h-oper-fc.grib2"
            gfs_base = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{token}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f018"
            ecmwf_meta = _request_metadata(ecmwf, provider_id="ecmwf_ifs025_official", opener=opener, timeout_sec=timeout_sec)
            gfs_meta = _request_metadata(gfs_base, provider_id="gfs_0p25_official", opener=opener, timeout_sec=timeout_sec)
            gfs_idx = _request_metadata(gfs_base + ".idx", provider_id="gfs_0p25_official", opener=opener, timeout_sec=timeout_sec)
            rows.append({
                "run_date": f"{token[:4]}-{token[4:6]}-{token[6:8]}",
                "run_cycle_utc": cycle,
                "ecmwf": ecmwf_meta,
                "gfs_grib": gfs_meta,
                "gfs_index": gfs_idx,
                "gfs_range_extraction_possible": gfs_meta.get("status") == "AVAILABLE" and gfs_idx.get("status") == "AVAILABLE",
            })

    icon_listings = {
        cycle: _request_metadata(
            f"https://opendata.dwd.de/weather/nwp/icon/grib/{cycle}/",
            provider_id="icon_global_official",
            opener=opener,
            timeout_sec=timeout_sec,
            method="GET",
            read_listing=True,
        )
        for cycle in cycles
    }
    icon_exact_file_probes: list[dict[str, Any]] = []
    for token in dates:
        for cycle in cycles:
            for forecast_hour in ICON_EXACT_PROBE_FORECAST_HOURS:
                url = _icon_t2m_url(
                    run_date_token=token,
                    cycle=cycle,
                    forecast_hour=forecast_hour,
                )
                metadata = _request_metadata(
                    url,
                    provider_id="icon_global_official",
                    opener=opener,
                    timeout_sec=timeout_sec,
                    method="HEAD",
                )
                icon_exact_file_probes.append({
                    "run_date": f"{token[:4]}-{token[4:6]}-{token[6:8]}",
                    "run_cycle_utc": cycle,
                    "forecast_hour": forecast_hour,
                    **metadata,
                })
    # Preserve the original singular field for existing consumers while
    # exposing every requested current-cycle listing to the audit.
    icon_listing = icon_listings[cycles[0]]
    body = {
        "schema": SCHEMA,
        "probed_at_utc": probed_at_utc or _utc_now(),
        "requested_run_dates": [f"{t[:4]}-{t[4:6]}-{t[6:8]}" for t in dates],
        "requested_run_cycles_utc": list(cycles),
        "sources": {
            "ecmwf_ifs025_official": {
                "archive_kind": "date_addressable_open_data",
                "historical_rows_with_native_metadata": sum(row["ecmwf"].get("status") == "AVAILABLE" for row in rows),
                "requested_dates": len(dates),
                "requested_run_cycles": len(cycles),
            },
            "gfs_0p25_official": {
                "archive_kind": "date_addressable_s3_pds_with_idx",
                "historical_rows_with_native_metadata": sum(row["gfs_range_extraction_possible"] for row in rows),
                "requested_dates": len(dates),
                "requested_run_cycles": len(cycles),
                "range_only_evidence": True,
            },
            "icon_global_official": {
                "archive_kind": "current_run_directory_only",
                "date_addressable_historical_archive": False,
                "listing_status": icon_listing.get("status"),
                "listing_status_by_cycle": {cycle: listing.get("status") for cycle, listing in icon_listings.items()},
                "exact_file_probe_count": len(icon_exact_file_probes),
                "exact_file_probe_available_count": sum(
                    row.get("status") == "AVAILABLE" for row in icon_exact_file_probes
                ),
                "exact_file_probe_unavailable_count": sum(
                    row.get("status") == "UNAVAILABLE" for row in icon_exact_file_probes
                ),
                "exact_file_probe_error_count": sum(
                    row.get("status") == "ERROR" for row in icon_exact_file_probes
                ),
                "exact_file_probe_urls_are_date_stamped": True,
            },
        },
        "date_probes": rows,
        "icon_current_listing": icon_listing,
        "icon_current_listings": icon_listings,
        "icon_exact_file_probes": icon_exact_file_probes,
        "boundary": {
            "offline_research_only": True,
            "raw_payload_downloaded": False,
            "raw_payload_bytes_retained": 0,
            "writes_target_database": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "promotion_allowed": False,
        },
    }
    body["receipt_sha256"] = content_sha256(body)
    return body


__all__ = [
    "ALLOWED_HOSTS",
    "ICON_EXACT_PROBE_FORECAST_HOURS",
    "MAX_INDEX_BYTES",
    "SCHEMA",
    "SUPPORTED_RUN_CYCLES",
    "probe_official_archives",
]
