#!/usr/bin/env python3
"""Capture bounded official AWC TAF/truth rows for offline TAF calibration.

The AWC ``date`` parameter is the historical address.  Each event day uses the
latest TAF available at a fixed settlement-relative decision anchor and the
official AWC METAR/SPECI rows inside that market's settlement window.  The
script writes one compact research artifact only; it never opens production
SQLite, starts a service, or calls an order gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_awc_taf_calibration_dataset_v1"
TAF_ENDPOINT = "https://aviationweather.gov/api/data/taf"
METAR_ENDPOINT = "https://aviationweather.gov/api/data/metar"
USER_AGENT = "weather-target-research/1.0 (bounded official AWC TAF OOS capture)"
MAX_DAYS = 90
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MIN_REQUEST_INTERVAL_SEC = 0.25
TAF_TX_RE = re.compile(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b")
PRECIP_RE = re.compile(r"(?:^|\s)(?:[-+]?DZ|[-+]?RA|[-+]?SN|SG|PL|GR|GS|SH|TS)")
SKY_FRACTION = {
    "SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "NCD": 0.0,
    "FEW": 0.125, "SCT": 0.375, "BKN": 0.75, "OVC": 1.0,
    "VV": 1.0,
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _event_window(event_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    local = ZoneInfo(timezone_name)
    day = date.fromisoformat(event_date)
    start = datetime.combine(day, wall_time.min, tzinfo=local).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), wall_time.min, tzinfo=local).astimezone(timezone.utc)
    return start, end


def _fetch_json(url: str, *, timeout_sec: int) -> tuple[list[dict[str, Any]], str, int]:
    completed = subprocess.run(
        [
            "/usr/bin/curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--noproxy",
            "aviationweather.gov",
            "--max-time",
            str(timeout_sec),
            "--max-filesize",
            str(MAX_RESPONSE_BYTES),
            "--header",
            f"User-Agent: {USER_AGENT}",
            "--header",
            "Accept: application/json",
            url,
        ],
        check=False,
        capture_output=True,
    )
    raw = bytes(completed.stdout)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace")[:240].strip()
        raise ContractError(
            f"awc_taf_calibration_curl_failed_rc_{completed.returncode}:{detail}"
        )
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ContractError("awc_taf_calibration_response_too_large")
    if not raw.strip():
        return [], hashlib.sha256(raw).hexdigest(), len(raw)
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ContractError("awc_taf_calibration_response_shape_invalid")
    return (
        [dict(row) for row in payload if isinstance(row, Mapping)],
        hashlib.sha256(raw).hexdigest(),
        len(raw),
    )


def _tx(value: str, *, issue_time: datetime, start: datetime, end: datetime) -> tuple[float, str]:
    candidates: list[tuple[datetime, float]] = []
    for raw_temperature, raw_day, raw_hour in TAF_TX_RE.findall(value):
        temperature = -float(raw_temperature[1:]) if raw_temperature.startswith("M") else float(raw_temperature)
        wanted_day = int(raw_day)
        wanted_hour = int(raw_hour)
        cursor = start - timedelta(days=35)
        stop = end + timedelta(days=35)
        while cursor.date() <= stop.date():
            if cursor.day == wanted_day:
                candidate = datetime(
                    cursor.year, cursor.month, cursor.day, wanted_hour,
                    tzinfo=timezone.utc,
                )
                if issue_time <= candidate <= end + timedelta(days=1):
                    candidates.append((candidate, temperature))
            cursor += timedelta(days=1)
    in_window = [item for item in candidates if start <= item[0] < end]
    if not in_window:
        raise ContractError("awc_taf_tx_for_settlement_day_missing")
    valid_at, temperature = min(in_window, key=lambda item: abs((item[0] - issue_time).total_seconds()))
    return temperature, _iso(valid_at)


def _forecast_features(
    taf: Mapping[str, Any],
    *,
    event_date: str,
    timezone_name: str,
) -> tuple[float, float, float]:
    local = ZoneInfo(timezone_name)
    day = date.fromisoformat(event_date)
    # Daytime warming window; TX itself remains the main calibrated signal.
    peak_start = datetime.combine(day, wall_time(6, 0), tzinfo=local).astimezone(timezone.utc)
    peak_end = datetime.combine(day, wall_time(15, 0), tzinfo=local).astimezone(timezone.utc)
    weighted_cloud = 0.0
    weighted_precip = 0.0
    weighted_wind = 0.0
    total_weight = 0.0
    for row in taf.get("fcsts", []):
        if not isinstance(row, Mapping):
            continue
        try:
            start = _parse_utc(row.get("timeFrom"))
            end = _parse_utc(row.get("timeTo"))
        except (TypeError, ValueError, OverflowError):
            continue
        overlap = max(
            0.0,
            (min(end, peak_end) - max(start, peak_start)).total_seconds(),
        )
        if overlap <= 0.0:
            continue
        covers = [
            SKY_FRACTION.get(str(item.get("cover") or "").upper(), 0.0)
            for item in row.get("clouds", [])
            if isinstance(item, Mapping)
        ]
        cloud = max(covers, default=0.0)
        wx = str(row.get("wxString") or "")
        probability_raw = row.get("probability")
        if probability_raw is not None:
            probability = max(0.0, min(1.0, float(probability_raw) / 100.0))
        else:
            probability = 1.0 if PRECIP_RE.search(wx) else 0.0
        wind_kt = float(row.get("wspd") or 0.0)
        weighted_cloud += overlap * cloud
        weighted_precip += overlap * probability
        weighted_wind += overlap * max(0.0, wind_kt * 0.514444)
        total_weight += overlap
    if total_weight <= 0.0:
        raise ContractError("awc_taf_peak_window_features_missing")
    return (
        weighted_cloud / total_weight,
        weighted_precip / total_weight,
        weighted_wind / total_weight,
    )


def _taf_row(
    rows: list[dict[str, Any]],
    *,
    station_id: str,
    start: datetime,
    end: datetime,
    event_date: str,
    timezone_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [
        row for row in rows
        if str(row.get("icaoId") or "").strip().upper() == station_id
    ]
    if not candidates:
        raise ContractError("awc_taf_history_row_missing")
    taf = max(candidates, key=lambda row: str(row.get("issueTime") or ""))
    raw_taf = str(taf.get("rawTAF") or "").strip()
    issue_time = _parse_utc(taf.get("issueTime"))
    available_time = _parse_utc(taf.get("dbPopTime") or taf.get("issueTime"))
    if available_time >= end:
        raise ContractError("awc_taf_not_available_before_truth_close")
    tx_c, tx_valid_at = _tx(raw_taf, issue_time=issue_time, start=start, end=end)
    cloud, precip, wind = _forecast_features(
        taf,
        event_date=event_date,
        timezone_name=timezone_name,
    )
    compact = {
        "issue_time_utc": _iso(issue_time),
        "available_time_utc": _iso(available_time),
        "tx_c": tx_c,
        "tx_valid_at_utc": tx_valid_at,
        "cloud_cover_fraction": cloud,
        "precipitation_probability": precip,
        "wind_speed_mps": wind,
        "raw_taf": raw_taf,
        "raw_taf_sha256": hashlib.sha256(raw_taf.encode("utf-8")).hexdigest(),
    }
    return compact, taf


def _truth_rows(
    rows: list[dict[str, Any]],
    *,
    station_id: str,
    start: datetime,
    end: datetime,
) -> tuple[float, list[dict[str, Any]]]:
    compact: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("icaoId") or "").strip().upper() != station_id:
            continue
        if row.get("temp") is None:
            continue
        try:
            observed = _parse_utc(row.get("obsTime") or row.get("reportTime"))
            temperature = float(row.get("temp"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(temperature) or not start <= observed < end:
            continue
        kind = str(row.get("metarType") or "METAR").upper()
        if kind not in {"METAR", "SPECI"}:
            continue
        compact.append({
            "observed_at_utc": _iso(observed),
            "received_at_utc": str(row.get("receiptTime") or ""),
            "report_type": kind,
            "temperature_c": temperature,
            "raw_report": str(row.get("rawOb") or "").strip(),
        })
    if not compact:
        raise ContractError("awc_taf_calibration_truth_missing")
    compact.sort(key=lambda row: (row["observed_at_utc"], row["report_type"], row["raw_report"]))
    return max(float(row["temperature_c"]) for row in compact), compact


def capture(
    *,
    station_id: str,
    timezone_name: str,
    start_date: str,
    end_date: str,
    timeout_sec: int,
    request_interval_sec: float,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last:
        raise ContractError("awc_taf_calibration_date_range_invalid")
    count = (last - first).days + 1
    if count > MAX_DAYS:
        raise ContractError("awc_taf_calibration_date_range_too_large")
    station = station_id.strip().upper()
    now = datetime.now(timezone.utc)
    total_bytes = int(
        ((existing or {}).get("resource_bounds") or {}).get(
            "actual_response_bytes", 0
        )
    )
    if existing is not None:
        existing_end_text = str(existing.get("end_date") or "")
        try:
            existing_end = date.fromisoformat(existing_end_text)
        except ValueError as exc:
            raise ContractError(
                "awc_taf_calibration_resume_scope_mismatch"
            ) from exc
        if (
            existing.get("schema") != SCHEMA
            or existing.get("station_id") != station
            or existing.get("settlement_timezone") != timezone_name
            or existing.get("start_date") != start_date
            or existing_end < first
            or existing_end > last
        ):
            raise ContractError("awc_taf_calibration_resume_scope_mismatch")
    samples: list[dict[str, Any]] = [
        dict(item)
        for item in (existing or {}).get("samples", [])
        if isinstance(item, Mapping)
    ]
    excluded_samples: list[dict[str, str]] = [
        dict(item)
        for item in (existing or {}).get("excluded_samples", [])
        if isinstance(item, Mapping)
    ]
    blockers: list[dict[str, str]] = []
    completed_dates = {str(item.get("event_date") or "") for item in samples}
    completed_dates.update(
        str(item.get("event_date") or "") for item in excluded_samples
    )
    cursor = first
    last_request_at = 0.0
    while cursor <= last:
        assert_process_memory_budget()
        event_date = cursor.isoformat()
        if event_date in completed_dates:
            cursor += timedelta(days=1)
            continue
        start, end = _event_window(event_date, timezone_name)
        if end > now:
            raise ContractError("awc_taf_calibration_open_truth_day_forbidden")
        # Fixed decision anchor prevents choosing a later TAF with hindsight.
        anchor = start + timedelta(hours=1)
        taf_url = TAF_ENDPOINT + "?" + urllib.parse.urlencode({
            "ids": station,
            "format": "json",
            "time": "issue",
            "date": _iso(anchor),
        })
        metar_url = METAR_ENDPOINT + "?" + urllib.parse.urlencode({
            "ids": station,
            "format": "json",
            "taf": "false",
            "hours": "24",
            "date": end.strftime("%Y%m%d_%H%M"),
        })
        try:
            wait = request_interval_sec - (time.monotonic() - last_request_at)
            if wait > 0:
                time.sleep(wait)
            taf_rows, taf_response_sha, taf_bytes = _fetch_json(
                taf_url, timeout_sec=timeout_sec
            )
            last_request_at = time.monotonic()
            wait = request_interval_sec - (time.monotonic() - last_request_at)
            if wait > 0:
                time.sleep(wait)
            metar_rows, metar_response_sha, metar_bytes = _fetch_json(
                metar_url, timeout_sec=timeout_sec
            )
            last_request_at = time.monotonic()
            total_bytes += taf_bytes + metar_bytes
            if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ContractError("awc_taf_calibration_total_response_cap_exceeded")
            taf, _ = _taf_row(
                taf_rows,
                station_id=station,
                start=start,
                end=end,
                event_date=event_date,
                timezone_name=timezone_name,
            )
            truth_tmax, observations = _truth_rows(
                metar_rows,
                station_id=station,
                start=start,
                end=end,
            )
            sample_identity = {
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "taf_issue_time_utc": taf["issue_time_utc"],
                "taf_raw_sha256": taf["raw_taf_sha256"],
                "truth_tmax_c": truth_tmax,
                "truth_rows_sha256": content_sha256(observations),
            }
            samples.append({
                "sample_id": "taf-sample-" + content_sha256(sample_identity)[:32],
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "settlement_window": {"start_utc": _iso(start), "end_utc": _iso(end)},
                "decision_anchor_utc": _iso(anchor),
                "taf": taf,
                "truth": {
                    "source_lane": "AWC",
                    "tmax_c": truth_tmax,
                    "observation_count": len(observations),
                    "observations": observations,
                },
                "receipts": {
                    "taf_source_url": taf_url,
                    "taf_response_sha256": taf_response_sha,
                    "metar_source_url": metar_url,
                    "metar_response_sha256": metar_response_sha,
                },
            })
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:160]}"
            if str(exc) in {
                "awc_taf_tx_for_settlement_day_missing",
                "awc_taf_peak_window_features_missing",
            }:
                excluded_samples.append({
                    "event_date": event_date,
                    "reason": reason,
                    "classification": "official_taf_structural_missing_field",
                })
            else:
                blockers.append({"event_date": event_date, "reason": reason})
        cursor += timedelta(days=1)
    samples.sort(key=lambda item: str(item.get("event_date") or ""))
    excluded_samples.sort(key=lambda item: str(item.get("event_date") or ""))
    identity = {
        "schema": SCHEMA,
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": station,
        "settlement_timezone": timezone_name,
        "start_date": start_date,
        "end_date": end_date,
        "sample_count": len(samples),
        "samples": samples,
        "excluded_sample_count": len(excluded_samples),
        "excluded_samples": excluded_samples,
        "blockers": blockers,
        "resource_bounds": {
            "max_days": MAX_DAYS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
            "actual_response_bytes": total_bytes,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--settlement-timezone", default="Asia/Seoul")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--timeout-sec", type=int, default=20)
    parser.add_argument("--request-interval-sec", type=float, default=MIN_REQUEST_INTERVAL_SEC)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed rows in the existing output and retry only transport blockers.",
    )
    args = parser.parse_args()
    if not 5 <= args.timeout_sec <= 60:
        raise SystemExit("timeout-sec must be in [5,60]")
    if not MIN_REQUEST_INTERVAL_SEC <= args.request_interval_sec <= 5.0:
        raise SystemExit(f"request-interval-sec must be in [{MIN_REQUEST_INTERVAL_SEC},5]")
    output = args.output.expanduser().resolve()
    free_bytes = shutil.disk_usage(output.parent if output.parent.exists() else ROOT).free
    if free_bytes < MIN_TARGET_FREE_BYTES + MAX_OUTPUT_BYTES:
        raise SystemExit("insufficient free disk for bounded TAF calibration capture")
    existing = None
    if args.resume and output.exists():
        loaded = load_bounded_json(output)
        if not isinstance(loaded, Mapping):
            raise SystemExit("resume artifact must be a JSON object")
        existing = loaded
    result = capture(
        station_id=args.station_id,
        timezone_name=args.settlement_timezone,
        start_date=args.start_date,
        end_date=args.end_date,
        timeout_sec=args.timeout_sec,
        request_interval_sec=args.request_interval_sec,
        existing=existing,
    )
    write_bounded_json(output, result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "schema": result["schema"],
        "sample_count": result["sample_count"],
        "excluded_sample_count": result["excluded_sample_count"],
        "blocker_count": len(result["blockers"]),
        "actual_response_bytes": result["resource_bounds"]["actual_response_bytes"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["sample_count"] >= 24 and not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
