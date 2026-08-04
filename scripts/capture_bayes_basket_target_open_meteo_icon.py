#!/usr/bin/env python3
"""Capture explicit historical aggregator NWP replay evidence from Open-Meteo.

Open-Meteo is an allowed aggregator/republisher for historical ICON and CMA
completion. GEM exact-run history uses the official Environment Canada HPFX
archive because Open-Meteo does not expose the required GEM run. This command
never writes a target database, never creates
native publication/arrival timestamps, never touches the live line, and keeps
raw bodies content-addressed under the isolated research tree.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_closed, event_day_window  # noqa: E402
from bayes_basket_target.model_sources import provider_role, provider_source_class  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_PROCESS_RSS_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from bayes_basket_target.settlement_time import (  # noqa: E402
    historical_registry_scope_settlement_timezone,
)
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402


SCHEMA = "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"
RECEIPT_SCHEMA = "bayes_basket_target_open_meteo_icon_receipt_v1"
PROVIDER_ID = "icon_global_open_meteo"
MODEL_ID = "ICON"
SOURCE_CLASS = "aggregator_republisher"
API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
SINGLE_RUN_API_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL_PARAM = "dwd_icon_global"
MODEL_PROFILES = {
    "ICON": {
        "provider_id": PROVIDER_ID,
        "model_param": MODEL_PARAM,
    },
    "CMA": {
        "provider_id": "cma_grapes_global_open_meteo",
        "model_param": "cma_grapes_global",
    },
    "AIFS": {
        "provider_id": "ecmwf_aifs_single_open_meteo",
        "model_param": "ecmwf_aifs025_single",
    },
    "ICON-EU": {
        "provider_id": "icon_eu_open_meteo",
        "model_param": "icon_eu",
    },
    "ICON-D2": {
        "provider_id": "icon_d2_open_meteo",
        "model_param": "icon_d2",
    },
}
VARIABLE = "temperature_2m"
DEFAULT_STATION_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_OUTPUT_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_open_meteo_icon_rebuild.json"
MAX_STATIONS = 45
MAX_DATES = 14
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_RAW_BYTES = 256 * 1024 * 1024
MAX_WORKERS = 4
CHUNK_BYTES = 64 * 1024
MIN_REBUILD_FREE_BYTES = 10 * 1024 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ContractError("open_meteo_icon_event_date_invalid") from exc


def _load_station_ids(path: Path, explicit: list[str] | None) -> list[str]:
    if explicit:
        stations = sorted({str(item).strip().upper() for item in explicit if str(item).strip()})
    else:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
        stations_payload = payload.get("stations") if isinstance(payload, Mapping) else payload
        if not isinstance(stations_payload, Mapping):
            raise ContractError("open_meteo_icon_station_map_invalid")
        stations = sorted({str(item).strip().upper() for item in stations_payload})
    if not stations or len(stations) > MAX_STATIONS:
        raise ContractError("open_meteo_icon_station_scope_invalid")
    for station in stations:
        station_coordinates(station)
    return stations


def _registry_timezone(
    path: Path,
    *,
    stations: list[str],
    event_dates: list[date],
) -> str:
    payload = load_bounded_json(path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("open_meteo_icon_timezone_registry_rows_missing")
    # This adapter is historical-only.  The requested weather date can
    # legitimately predate the first retained market row, so exact
    # station×event-date lookup would reject valid history.  Carry the
    # registry timezone backward only when it is invariant for every selected
    # station; the shared helper still rejects missing, conflicting, or mixed
    # timezone scopes.
    return historical_registry_scope_settlement_timezone(
        (row for row in rows if isinstance(row, Mapping)),
        station_ids=stations,
        event_dates=[item.isoformat() for item in event_dates],
    )


def _registry_timezones(
    path: Path,
    *,
    stations: list[str],
) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("open_meteo_icon_timezone_registry_rows_missing")
    values: dict[str, set[str]] = {station: set() for station in stations}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station in values and timezone_name:
            values[station].add(timezone_name)
    result: dict[str, str] = {}
    for station in stations:
        if len(values[station]) != 1:
            raise ContractError(
                f"open_meteo_icon_station_timezone_invalid:{station}"
            )
        result[station] = next(iter(values[station]))
    return result


def _requested_dates(args: argparse.Namespace) -> list[date]:
    values = {_parse_date(item) for item in (args.event_date or [])}
    if args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            raise ContractError("open_meteo_icon_date_range_incomplete")
        start, end = _parse_date(args.start_date), _parse_date(args.end_date)
        if end < start:
            raise ContractError("open_meteo_icon_date_range_invalid")
        values.update(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    dates = sorted(values)
    if not dates or len(dates) > MAX_DATES:
        raise ContractError("open_meteo_icon_event_date_count_invalid")
    return dates


def _target_dates(
    args: argparse.Namespace,
    *,
    as_of_utc: str,
    target_timezone: str,
) -> list[date]:
    dates = _requested_dates(args)
    for item in dates:
        assert_event_day_closed(
            item.isoformat(),
            as_of_utc=as_of_utc,
            target_timezone=target_timezone,
        )
    return dates


def _request_window(
    event_dates: list[date],
    *,
    target_timezone: str,
) -> tuple[str, str]:
    zone = ZoneInfo(target_timezone)
    starts = [datetime.combine(item, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc) for item in event_dates]
    ends = [start + timedelta(days=1) for start in starts]
    start_date = min(starts).date().isoformat()
    end_date = (max(ends) - timedelta(hours=1)).date().isoformat()
    return start_date, end_date


def _run_init(event_date: date, *, run_cycle: str | None = None, run_day_offset: int = 1) -> str:
    if run_cycle is None:
        # The stitched historical API does not expose a native DWD cycle.
        return (event_date - timedelta(days=1)).isoformat() + "T00:00:00Z"
    cycle = str(run_cycle).strip().upper()
    if cycle not in {"00Z", "06Z", "12Z", "18Z"}:
        raise ContractError("open_meteo_icon_run_cycle_invalid")
    if int(run_day_offset) not in {1, 2}:
        raise ContractError("open_meteo_icon_run_day_offset_invalid")
    run = datetime.combine(
        event_date - timedelta(days=int(run_day_offset)),
        datetime.min.time().replace(hour=int(cycle[:2])),
        tzinfo=timezone.utc,
    )
    return run.isoformat(timespec="minutes").replace("+00:00", "Z")


def _url(
    station: str,
    event_dates: list[date],
    *,
    run_cycle: str | None = None,
    run_day_offset: int = 1,
    target_timezone: str = "",
    model_id: str = MODEL_ID,
) -> str:
    normalized_model_id = str(model_id or "").strip().upper()
    profile = MODEL_PROFILES.get(normalized_model_id)
    if profile is None:
        raise ContractError("open_meteo_exact_run_model_unsupported")
    coordinates = station_coordinates(station)
    params: dict[str, str] = {
        "latitude": str(coordinates["latitude"]),
        "longitude": str(coordinates["longitude"]),
        "hourly": VARIABLE,
        "models": str(profile["model_param"]),
        "timezone": "UTC",
    }
    if run_cycle is None:
        start_date, end_date = _request_window(
            event_dates,
            target_timezone=target_timezone,
        )
        params.update({"start_date": start_date, "end_date": end_date})
        endpoint = API_URL
    else:
        if len(event_dates) != 1:
            raise ContractError("open_meteo_icon_single_run_one_event_date_required")
        params.update(
            {
                "run": _run_init(
                    event_dates[0],
                    run_cycle=run_cycle,
                    run_day_offset=run_day_offset,
                )[:-1],
                "forecast_days": "4",
            }
        )
        endpoint = SINGLE_RUN_API_URL
    return endpoint + "?" + urlencode(params)


def _fetch(
    station: str,
    event_dates: list[date],
    *,
    timeout_sec: float,
    run_cycle: str | None = None,
    run_day_offset: int = 1,
    target_timezone: str,
    model_id: str = MODEL_ID,
    proxy_url: str | None = None,
) -> tuple[str, bytes, str]:
    url = _url(
        station,
        event_dates,
        run_cycle=run_cycle,
        run_day_offset=run_day_offset,
        target_timezone=target_timezone,
        model_id=model_id,
    )
    marker = b"\n__OPEN_METEO_HTTP_STATUS__"
    proxy = (
        official_nwp_proxy_url()
        if proxy_url is None
        else str(proxy_url).strip()
    )
    transport_args = ["--proxy", proxy] if proxy else ["--noproxy", "*"]
    completed = subprocess.run(
        [
            "curl", *transport_args, "--fail-with-body", "--silent", "--show-error",
            "--connect-timeout", str(min(float(timeout_sec), 15.0)),
            "--max-time", str(float(timeout_sec)),
            "--max-filesize", str(MAX_RESPONSE_BYTES),
            "-A", "bayes-basket-target-open-meteo-icon/1",
            "-w", "\n__OPEN_METEO_HTTP_STATUS__%{http_code}", url,
        ],
        capture_output=True,
        timeout=float(timeout_sec) + 5.0,
        check=False,
    )
    output = bytes(completed.stdout or b"")
    marker_index = output.rfind(marker)
    if marker_index < 0:
        detail = bytes(completed.stderr or b"").decode("utf-8", errors="replace")[-256:]
        raise ContractError(f"open_meteo_icon_curl_failed:{detail or completed.returncode}")
    body = output[:marker_index]
    status = output[marker_index + len(marker):].strip().decode("ascii", errors="replace")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ContractError("open_meteo_icon_response_too_large")
    if status != "200" or completed.returncode != 0:
        raise ContractError(f"open_meteo_icon_http_{status or completed.returncode}")
    return url, body, status


def _persist_raw(
    output_root: Path,
    body: bytes,
    *,
    provider_id: str = PROVIDER_ID,
) -> Path:
    raw_root = (
        output_root / "raw"
        if output_root.name == provider_id
        else output_root / provider_id / "raw"
    )
    raw_root.mkdir(parents=True, exist_ok=True)
    digest = _sha256(body)
    destination = raw_root / f"{digest}.json"
    if destination.is_file():
        if _sha256(destination.read_bytes()) != digest:
            raise ContractError("open_meteo_icon_content_address_collision")
        return destination
    retained = sum(
        path.stat().st_size
        for path in raw_root.glob("*.json")
        if path.is_file() and not path.is_symlink()
    )
    if retained + len(body) > MAX_TOTAL_RAW_BYTES:
        raise ContractError("open_meteo_icon_provider_raw_budget_exceeded")
    if shutil.disk_usage(output_root).free < max(
        MIN_TARGET_FREE_BYTES,
        MIN_REBUILD_FREE_BYTES,
    ) + len(body):
        raise ContractError("open_meteo_icon_min_free_space_guard")
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(body)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _load_resume(
    path: Path | None,
    *,
    output_root: Path,
    event_dates: set[str],
    stations: set[str],
    target_timezone: str,
    provider_id: str = PROVIDER_ID,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[tuple[str, str]],
    str,
]:
    if path is None:
        return [], [], [], set(), ""
    resume_path = path.expanduser().resolve()
    if not resume_path.is_file():
        raise ContractError("open_meteo_icon_resume_missing")
    payload = load_bounded_json(resume_path)
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
        raise ContractError("open_meteo_icon_resume_schema_invalid")
    if str(payload.get("target_timezone") or "").strip() != target_timezone:
        raise ContractError("open_meteo_icon_resume_settlement_timezone_mismatch")
    raw_root = (
        output_root / "raw"
        if output_root.name == provider_id
        else output_root / provider_id / "raw"
    ).resolve()

    def safe_raw_ref(value: Any) -> bool:
        try:
            candidate = Path(str(value)).expanduser().resolve()
            return candidate.is_relative_to(raw_root) and candidate.is_file()
        except (OSError, TypeError, ValueError):
            return False

    rows: list[dict[str, Any]] = []
    for item in payload.get("model_rows") or []:
        if not isinstance(item, Mapping):
            continue
        station = str(item.get("station_id") or "").strip().upper()
        event_date = str(item.get("event_date") or "").strip()
        if station not in stations or event_date not in event_dates:
            continue
        if not safe_raw_ref(item.get("raw_object_ref")):
            raise ContractError("open_meteo_icon_resume_raw_reference_invalid")
        rows.append(dict(item))

    row_raw_refs = {
        (
            str(item.get("station_id") or "").strip().upper(),
            str(item.get("event_date") or "").strip(),
        ): item.get("raw_object_ref")
        for item in rows
    }

    curves: list[dict[str, Any]] = []
    for item in ((payload.get("curves") or {}).get("eligible_model_rows") or []):
        if not isinstance(item, Mapping):
            continue
        station = str(item.get("station_id") or "").strip().upper()
        event_date = str(item.get("event_date") or "").strip()
        if station in stations and event_date in event_dates:
            raw_ref = item.get("raw_object_ref") or row_raw_refs.get((station, event_date))
            if not safe_raw_ref(raw_ref):
                raise ContractError("open_meteo_icon_resume_curve_reference_invalid")
            curve = dict(item)
            curve["raw_object_ref"] = str(raw_ref)
            curves.append(curve)

    complete_keys = {
        (str(item.get("station_id") or "").strip().upper(), str(item.get("event_date") or "").strip())
        for item in curves
    }
    receipts = [
        dict(item)
        for item in payload.get("receipts") or []
        if isinstance(item, Mapping)
        and bool(item.get("event_dates"))
        and all(
            (
                str(item.get("station_id") or "").strip().upper(),
                str(event_date).strip(),
            )
            in complete_keys
            for event_date in (item.get("event_dates") or [])
        )
    ]
    resume_sha256 = _sha256(resume_path.read_bytes())
    return rows, curves, receipts, complete_keys, resume_sha256


def _canonicalize_response_body(body: bytes) -> bytes:
    """Drop volatile transport metadata before content-addressing the curve."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("open_meteo_icon_response_json_invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("hourly"), Mapping):
        raise ContractError("open_meteo_icon_hourly_missing")
    canonical = {
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "hourly_units": payload.get("hourly_units"),
        "hourly": payload["hourly"],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ContractError("open_meteo_icon_response_too_large")
    return encoded


def _utc_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("open_meteo_icon_time_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("open_meteo_icon_time_timezone_required")
    return parsed.astimezone(timezone.utc)


def _extract_station(
    *, station: str, event_dates: list[date], url: str, body: bytes, http_status: str,
    raw_path: Path, reconstructed_at: str, run_cycle: str | None = None,
    run_day_offset: int = 1, target_timezone: str,
    model_id: str = MODEL_ID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    normalized_model_id = str(model_id or "").strip().upper()
    profile = MODEL_PROFILES.get(normalized_model_id)
    if profile is None:
        raise ContractError("open_meteo_exact_run_model_unsupported")
    provider_id = str(profile["provider_id"])
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("open_meteo_icon_response_json_invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("hourly"), Mapping):
        raise ContractError("open_meteo_icon_hourly_missing")
    hourly = payload["hourly"]
    times, values = hourly.get("time"), hourly.get(VARIABLE)
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        raise ContractError("open_meteo_icon_hourly_shape_invalid")
    parsed: dict[str, float] = {}
    for raw_time, raw_value in zip(times, values):
        if raw_value is None:
            continue
        timestamp = _utc_timestamp(str(raw_time) + ("Z" if "T" in str(raw_time) and "+" not in str(raw_time) else ""))
        try:
            temperature = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ContractError("open_meteo_icon_temperature_invalid") from exc
        if not (-100.0 < temperature < 100.0):
            raise ContractError("open_meteo_icon_temperature_out_of_range")
        key = timestamp.isoformat().replace("+00:00", "Z")
        if key in parsed:
            raise ContractError("open_meteo_icon_duplicate_valid_time")
        parsed[key] = temperature

    model_rows: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for event_date in event_dates:
        window = event_day_window(
            event_date.isoformat(),
            as_of_utc=reconstructed_at,
            target_timezone=target_timezone,
        )
        start = _utc_timestamp(window["target_day_start_utc"])
        end = _utc_timestamp(window["target_day_end_utc"])
        points = [
            (valid, value) for valid, value in sorted(parsed.items())
            if start <= _utc_timestamp(valid) < end
        ]
        expected = int((end - start).total_seconds() // 3600)
        if len(points) != expected or any(
            (idx and _utc_timestamp(points[idx][0]) - _utc_timestamp(points[idx - 1][0]) != timedelta(hours=1))
            for idx in range(len(points))
        ):
            blockers.append({"station_id": station, "event_date": event_date.isoformat(), "reason": "open_meteo_icon_curve_incomplete", "point_count": len(points), "expected_point_count": expected})
            continue
        run_init = _run_init(
            event_date,
            run_cycle=run_cycle,
            run_day_offset=run_day_offset,
        )
        max_valid_time, max_temperature = max(points, key=lambda item: item[1])
        row = {
            "station_id": station,
            "event_date": event_date.isoformat(),
            "target_timezone": target_timezone,
            "provider_id": provider_id,
            "provider_role": provider_role(provider_id),
            "model_id": normalized_model_id,
            "model_run_initialization_utc": run_init,
            "valid_time_utc": max_valid_time,
            "temperature_2m_c": max_temperature,
            "forecast_daily_max_c": max_temperature,
            "sample_count": expected,
            "daily_curve_complete": True,
            "curve_valid_time_start_utc": points[0][0],
            "curve_valid_time_end_utc": points[-1][0],
            "published_at_utc": "",
            "received_at_utc": "",
            "raw_message_sha256": _sha256(body),
            "receipt_sha256": "",
            "source_url": url,
            "raw_object_ref": str(raw_path.resolve()),
            "source_class": SOURCE_CLASS,
            "aggregator_or_republisher": True,
            "open_meteo": True,
            "capture_mode": "historical_rebuild",
            "reconstructed_at_utc": reconstructed_at,
            "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": False,
            "native_arrival_time_observed": False,
            "run_initialization_provenance": (
                "aggregator_single_run_parameter_explicit"
                if run_cycle is not None
                else "aggregator_replay_bucket_not_native_cycle"
            ),
        }
        point_rows = [row]
        curves.append({
            "station_id": station,
            "event_date": event_date.isoformat(),
            "provider_id": provider_id,
            "model_id": normalized_model_id,
            "model_run_initialization_utc": run_init,
            "source_class": SOURCE_CLASS,
            "aggregator_or_republisher": True,
            "open_meteo": True,
            "temperature_point_count": expected,
            "valid_time_start_utc": points[0][0],
            "valid_time_end_utc": points[-1][0],
            "forecast_daily_max_c": max_temperature,
            "raw_message_sha256": _sha256(body),
            "raw_object_ref": str(raw_path.resolve()),
            "complete": True,
        })
        model_rows.extend(point_rows)
    receipt_identity = {
        "schema": RECEIPT_SCHEMA,
        "provider_id": provider_id,
        "model_id": normalized_model_id,
        "station_id": station,
        "event_dates": [item.isoformat() for item in event_dates],
        "run_cycle": str(run_cycle).strip().upper() if run_cycle is not None else None,
        "run_day_offset": int(run_day_offset) if run_cycle is not None else None,
        "run_initializations_utc": [
            _run_init(item, run_cycle=run_cycle, run_day_offset=run_day_offset)
            for item in event_dates
        ],
        "source_url": url,
        "source_class": SOURCE_CLASS,
        "aggregator_or_republisher": True,
        "open_meteo": True,
        "raw_message_sha256": _sha256(body),
        "raw_object_ref": str(raw_path.resolve()),
        "http_status": http_status,
        "capture_mode": "historical_rebuild",
        "historical_arrival_time_observed": False,
        "native_publication_time_observed": False,
        "reconstructed_at_utc": reconstructed_at,
        "production_authority": False,
        "live_order_authorized": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    receipt_sha = content_sha256(receipt_identity)
    for row in model_rows:
        row["receipt_sha256"] = receipt_sha
    return model_rows, curves, {**receipt_identity, "receipt_sha256": receipt_sha, "blockers": blockers}


def capture(
    *, event_dates: list[date], stations: list[str], as_of_utc: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT, output_path: Path = DEFAULT_OUTPUT,
    timeout_sec: float = 30.0, resume_path: Path | None = None,
    run_cycle: str | None = None, run_day_offset: int = 1,
    max_workers: int = MAX_WORKERS,
    target_timezone: str,
    station_timezones: Mapping[str, str] | None = None,
    model_id: str = MODEL_ID,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    normalized_model_id = str(model_id or "").strip().upper()
    profile = MODEL_PROFILES.get(normalized_model_id)
    if profile is None:
        raise ContractError("open_meteo_exact_run_model_unsupported")
    provider_id = str(profile["provider_id"])
    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    reconstructed_at = _now()
    output_root = output_root.expanduser().resolve()
    normalized_timezones = {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in (station_timezones or {}).items()
    }
    if normalized_timezones:
        if set(normalized_timezones) != set(stations):
            raise ContractError(
                "open_meteo_icon_station_timezone_scope_mismatch"
            )
    else:
        normalized_timezones = {
            station: target_timezone for station in stations
        }
    unique_timezones = sorted(set(normalized_timezones.values()))
    receipt_timezone = (
        "REGISTRY_BOUND_PER_STATION"
        if len(unique_timezones) > 1
        else unique_timezones[0]
    )
    event_date_values = {item.isoformat() for item in event_dates}
    resumed_rows, resumed_curves, resumed_receipts, reusable_keys, resume_sha256 = _load_resume(
        resume_path,
        output_root=output_root,
        event_dates=event_date_values,
        stations=set(stations),
        target_timezone=receipt_timezone,
        provider_id=provider_id,
    )
    reusable_stations = {
        station for station in stations
        if all((station, event_date) in reusable_keys for event_date in event_date_values)
    }
    capture_stations = [station for station in stations if station not in reusable_stations]
    request_groups = [
        (station, [event_date])
        for station in capture_stations
        for event_date in event_dates
        if (station, event_date.isoformat()) not in reusable_keys
    ] if run_cycle is not None else [
        (station, event_dates)
        for station in capture_stations
    ]
    results: dict[tuple[str, tuple[date, ...]], tuple[str, bytes, str] | Exception] = {}
    worker_count = int(max_workers)
    if worker_count < 1 or worker_count > MAX_WORKERS:
        raise ContractError("open_meteo_icon_max_workers_invalid")
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="open-meteo-icon") as executor:
        futures = {
            executor.submit(
                _fetch,
                station,
                group_dates,
                timeout_sec=timeout_sec,
                run_cycle=run_cycle,
                run_day_offset=run_day_offset,
                target_timezone=normalized_timezones[station],
                model_id=normalized_model_id,
                proxy_url=proxy_url,
            ): (station, tuple(group_dates))
            for station, group_dates in request_groups
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except (TimeoutError, OSError, subprocess.SubprocessError, ContractError, ValueError) as exc:
                results[key] = exc

    model_rows: list[dict[str, Any]] = list(resumed_rows)
    curves: list[dict[str, Any]] = list(resumed_curves)
    receipts: list[dict[str, Any]] = list(resumed_receipts)
    blockers: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {"CAPTURED": len(reusable_stations)} if reusable_stations else {}
    for station, group_dates in request_groups:
        result = results.get((station, tuple(group_dates)))
        if isinstance(result, Exception) or result is None:
            blockers.append({
                "station_id": station,
                "event_dates": [item.isoformat() for item in group_dates],
                "reason": "open_meteo_icon_fetch_failed",
                "detail": str(result)[:256] if isinstance(result, Exception) else "missing_result",
            })
            status_counts["FETCH_FAILED"] = status_counts.get("FETCH_FAILED", 0) + 1
            continue
        url, body, http_status = result
        try:
            canonical_body = _canonicalize_response_body(body)
            raw_path = _persist_raw(
                output_root,
                canonical_body,
                provider_id=provider_id,
            )
            station_rows, station_curves, receipt = _extract_station(
                station=station, event_dates=group_dates, url=url, body=canonical_body,
                http_status=http_status, raw_path=raw_path, reconstructed_at=reconstructed_at,
                run_cycle=run_cycle, run_day_offset=run_day_offset,
                target_timezone=normalized_timezones[station],
                model_id=normalized_model_id,
            )
            receipts.append(receipt)
            model_rows.extend(station_rows)
            curves.extend(station_curves)
            blockers.extend(receipt.pop("blockers", []))
            status_counts["CAPTURED"] = status_counts.get("CAPTURED", 0) + 1
        except (ContractError, OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append({"station_id": station, "reason": "open_meteo_icon_decode_failed", "detail": str(exc)})
            status_counts["DECODE_FAILED"] = status_counts.get("DECODE_FAILED", 0) + 1

    payload = {
        "schema": SCHEMA,
        "provider_id": provider_id,
        "model_id": normalized_model_id,
        "target_timezone": receipt_timezone,
        "station_timezones": dict(sorted(normalized_timezones.items())),
        "event_dates": [item.isoformat() for item in event_dates],
        "station_ids": stations,
        "station_count": len(stations),
        "source": {
            "source_url_template": SINGLE_RUN_API_URL if run_cycle is not None else API_URL,
            "model_parameter": str(profile["model_param"]),
            "hourly_variable": VARIABLE,
            "source_class": SOURCE_CLASS,
            "aggregator_or_republisher": True,
            "open_meteo": True,
            "native_dwd_arrival_time_available": False,
            "single_run_parameter_explicit": run_cycle is not None,
            "run_cycle": str(run_cycle).strip().upper() if run_cycle is not None else None,
            "run_day_offset": int(run_day_offset) if run_cycle is not None else None,
        },
        "reconstructed_at_utc": reconstructed_at,
        "resume_evidence": {
            "path": str(resume_path.expanduser().resolve()) if resume_path is not None else None,
            "sha256": resume_sha256,
            "reused_station_count": len(reusable_stations),
            "reused_station_ids": sorted(reusable_stations),
            "reused_station_day_count": len(reusable_keys),
        },
        "model_rows": model_rows,
        "curves": {
            "complete_curve_count": len(curves),
            "eligible_model_rows": curves,
            "complete_station_day_keys": sorted({f"{item['station_id']}|{item['event_date']}" for item in curves}),
        },
        "receipts": receipts,
        "blockers": blockers[:1000],
        "status_counts": status_counts,
        "checks": {
            "all_requested_stations_captured": len({item["station_id"] for item in curves}) == len(stations) and not blockers,
            "all_curves_complete": bool(curves) and len(curves) == len(stations) * len(event_dates),
            "historical_replay_eligible": bool(curves),
            "prospective_strict_asof_eligible": False,
            "native_arrival_index_eligible": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "protected_production_line_touched": False,
        },
        "resource_budget": {
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_total_raw_bytes": MAX_TOTAL_RAW_BYTES,
            "min_free_bytes": max(
                MIN_TARGET_FREE_BYTES,
                MIN_REBUILD_FREE_BYTES,
            ),
            "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
            "worker_count": worker_count,
            "max_workers": MAX_WORKERS,
            "request_count": len(request_groups),
        },
        "boundary": {
            "target_only": True,
            "offline_research_only": True,
            "aggregator_cannot_be_native_arrival": True,
            "production_calibration_promotion_allowed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "protected_production_line_touched": False,
        },
    }
    payload["receipt_sha256"] = content_sha256(payload)
    write_bounded_json(output_path.expanduser().resolve(), payload)
    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    return {
        "schema": SCHEMA,
        "state": "READY" if payload["checks"]["all_curves_complete"] and not blockers else "PARTIAL",
        "output": str(output_path.expanduser().resolve()),
        "output_root": str(output_root),
        "event_dates": payload["event_dates"],
        "station_count": len(stations),
        "complete_curve_count": len(curves),
        "expected_curve_count": len(stations) * len(event_dates),
        "blocker_count": len(blockers),
        "status_counts": status_counts,
        "receipt_sha256": payload["receipt_sha256"],
        "historical_replay_eligible": True,
        "prospective_strict_asof_eligible": False,
        "native_arrival_index_eligible": False,
        "protected_production_line_touched": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--station-map", type=Path, default=DEFAULT_STATION_MAP)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume-evidence", type=Path, help="Reuse verified complete station-day curves from a prior bounded receipt.")
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--run-cycle", choices=["00Z", "06Z", "12Z", "18Z"])
    parser.add_argument("--run-day-offset", type=int, default=1, choices=[1, 2])
    parser.add_argument(
        "--transport",
        choices=("configured_proxy", "direct"),
        default="configured_proxy",
        help="Use the audited proxy config or an explicit direct connection.",
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PROFILES),
        default=MODEL_ID,
    )
    args = parser.parse_args()
    as_of_utc = args.as_of_utc or _now()
    stations = _load_station_ids(args.station_map, args.station_id)
    requested_dates = _requested_dates(args)
    station_timezones = _registry_timezones(
        args.timezone_registry,
        stations=stations,
    )
    for item in requested_dates:
        for timezone_name in sorted(set(station_timezones.values())):
            assert_event_day_closed(
                item.isoformat(),
                as_of_utc=as_of_utc,
                target_timezone=timezone_name,
            )
    dates = requested_dates
    target_timezone = (
        "REGISTRY_BOUND_PER_STATION"
        if len(set(station_timezones.values())) > 1
        else next(iter(station_timezones.values()))
    )
    result = capture(
        event_dates=dates, stations=stations, as_of_utc=as_of_utc,
        output_root=args.output_root, output_path=args.output, timeout_sec=args.timeout_sec,
        resume_path=args.resume_evidence,
        run_cycle=args.run_cycle, run_day_offset=args.run_day_offset,
        max_workers=args.max_workers,
        target_timezone=target_timezone,
        station_timezones=station_timezones,
        model_id=args.model,
        proxy_url=("" if args.transport == "direct" else None),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
