"""Bounded native-first probes for the deterministic republisher P0 lane.

The probe is deliberately separate from the existing native collectors.  It
does not download GRIB payloads, create native-arrival authority, or write the
SSOT.  Its only job is to prove whether the exact AIFS Single and ICON Global
deterministic valid-time cohort is complete, terminally unavailable/invalid/
late, or indeterminate at one decision clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .station_registry import station_coordinates


SCHEMA = "republisher_native_forward_probe_v1"
DEFAULT_PROXY = "http://127.0.0.1:1082"
MAX_AIFS_INDEX_BYTES = 1024 * 1024
MAX_ICON_HEAD_BYTES = 0
MAX_REQUESTS = 64
NATIVE_RECEIPT_TTL_SECONDS = 300
TERMINAL_FALLBACK_STATUSES = frozenset(
    {"NATIVE_UNAVAILABLE", "NATIVE_INVALID", "NATIVE_LATE"}
)
INDETERMINATE_HTTP_STATUSES = frozenset(
    {301, 302, 303, 307, 308, 401, 403, 408, 409, 425, 429, 500, 501, 502, 503, 504}
)

PRODUCTS: dict[str, dict[str, Any]] = {
    "AIFS": {
        "native_provider_id": "ecmwf_aifs025_official",
        "native_product_id": "ECMWF_AIFS_SINGLE_0P25",
        "cadence_hours": 6,
        "wait_hours": 8,
    },
    "ICON": {
        "native_provider_id": "icon_global_official",
        "native_product_id": "DWD_ICON_GLOBAL",
        "cadence_hours": 1,
        "wait_hours": 6,
    },
}


def _utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"native_forward_probe_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_forward_probe_{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _proxy(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 1082
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError("native_forward_probe_proxy_invalid")
    return str(value).strip()


def _event_window(event_date: str, settlement_timezone: str) -> tuple[datetime, datetime]:
    try:
        local_start = datetime.fromisoformat(str(event_date)).replace(
            tzinfo=ZoneInfo(str(settlement_timezone))
        )
    except Exception as exc:
        raise ContractError("native_forward_probe_event_scope_invalid") from exc
    return (
        local_start.astimezone(timezone.utc),
        (local_start + timedelta(days=1)).astimezone(timezone.utc),
    )


def _candidate_run(event_date: str, settlement_timezone: str) -> datetime:
    start, _ = _event_window(event_date, settlement_timezone)
    return start.replace(hour=(start.hour // 6) * 6, minute=0, second=0, microsecond=0)


def _aifs_urls(run: datetime, forecast_hour: int) -> tuple[str, str]:
    prefix = (
        "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/"
        f"{run:%Y%m%d}/{run:%H}z/aifs-single/0p25/oper/"
        f"{run:%Y%m%d%H}0000-{forecast_hour}h-oper-fc"
    )
    return prefix + ".grib2", prefix + ".index"


def _icon_url(run: datetime, forecast_hour: int) -> str:
    return (
        f"https://opendata.dwd.de/weather/nwp/icon/grib/{run:%H}/t_2m/"
        f"icon_global_icosahedral_single-level_{run:%Y%m%d%H}_"
        f"{forecast_hour:03d}_T_2M.grib2.bz2"
    )


def build_native_probe_plan(
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    checked_at_utc: str,
) -> dict[str, Any]:
    """Build exact AIFS/ICON cohorts without network access."""

    station = str(station_id or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", station):
        raise ContractError("native_forward_probe_station_invalid")
    checked = _utc(checked_at_utc, "checked_at_utc")
    start, end = _event_window(event_date, settlement_timezone)
    run = _candidate_run(event_date, settlement_timezone)
    products: list[dict[str, Any]] = []
    for model in ("AIFS", "ICON"):
        spec = PRODUCTS[model]
        cadence = int(spec["cadence_hours"])
        objects: list[dict[str, Any]] = []
        ordinal = 0
        lead = 0
        while run + timedelta(hours=lead) < end:
            valid = run + timedelta(hours=lead)
            if valid < start:
                lead += cadence
                continue
            if model == "AIFS":
                object_url, probe_url = _aifs_urls(run, lead)
                method = "GET"
            else:
                object_url = _icon_url(run, lead)
                probe_url = object_url
                method = "HEAD"
            objects.append(
                {
                    "ordinal": ordinal,
                    "valid_time_utc": _iso(valid),
                    "forecast_hour": lead,
                    "object_url": object_url,
                    "probe_url": probe_url,
                    "method": method,
                }
            )
            ordinal += 1
            lead += cadence
        if (
            (model == "ICON" and len(objects) not in {23, 24, 25})
            or (model == "AIFS" and len(objects) not in {3, 4, 5})
        ):
            raise ContractError("native_forward_probe_cohort_cardinality_invalid")
        policy = {
            "model": model,
            "station_id": station,
            "event_date": str(event_date),
            "settlement_timezone": str(settlement_timezone),
            "candidate_run_initialization_utc": _iso(run),
            "selection_rule": "latest_6h_cycle_at_or_before_station_day_start_v1",
        }
        deadline_policy = {
            "model": model,
            "candidate_run_initialization_utc": _iso(run),
            "wait_hours": int(spec["wait_hours"]),
            "wait_deadline_utc": _iso(run + timedelta(hours=int(spec["wait_hours"]))),
        }
        products.append(
            {
                "upstream_model_id": model,
                "native_provider_id": spec["native_provider_id"],
                "native_product_id": spec["native_product_id"],
                "candidate_run_initialization_utc": _iso(run),
                "candidate_cycle_policy_sha256": content_sha256(policy),
                "wait_deadline_policy_sha256": content_sha256(deadline_policy),
                "wait_deadline_utc": deadline_policy["wait_deadline_utc"],
                "required_objects": objects,
                "required_object_count": len(objects),
            }
        )
    body = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": str(event_date),
        "settlement_timezone": str(settlement_timezone),
        "checked_at_utc": _iso(checked),
        "station_scope_sha256": content_sha256(
            {
                "station_id": station,
                "event_date": str(event_date),
                "settlement_timezone": str(settlement_timezone),
            }
        ),
        "products": products,
    }
    return {**body, "content_sha256": content_sha256(body)}


def strict_proxy_request(
    method: str,
    url: str,
    *,
    proxy: str = DEFAULT_PROXY,
    max_bytes: int,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Perform one request through 127.0.0.1:1082 with no direct retry."""

    proxy = _proxy(proxy)
    method = str(method).upper()
    if method not in {"GET", "HEAD"}:
        raise ContractError("native_forward_probe_method_invalid")
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ContractError("native_forward_probe_url_invalid")
    requested = datetime.now(timezone.utc)
    try:
        with httpx.Client(
            proxy=proxy,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": "weather-republisher-native-probe/1"},
        ) as client:
            with client.stream(method, url) as response:
                started = datetime.now(timezone.utc)
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    raw.extend(chunk)
                    if len(raw) > int(max_bytes):
                        raise ContractError("native_forward_probe_response_too_large")
                completed = datetime.now(timezone.utc)
                headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in {"content-length", "content-type", "etag", "last-modified"}
                }
                return {
                    "http_status": int(response.status_code),
                    "requested_at_utc": _iso(requested),
                    "response_started_at_utc": _iso(started),
                    "response_completed_at_utc": _iso(completed),
                    "response_headers": headers,
                    "response_bytes": len(raw),
                    "raw_object_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
                    "_raw_body": bytes(raw),
                }
    except ContractError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise ContractError(
            "native_forward_probe_indeterminate_transport:" + type(exc).__name__
        ) from exc


def _aifs_semantic(body: bytes, forecast_hour: int) -> str:
    try:
        rows = [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ContractError("native_forward_probe_aifs_index_invalid") from exc
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("model") or "") == "aifs-single"
        and str(row.get("stream") or "") == "oper"
        and str(row.get("type") or "") == "fc"
        and str(row.get("levtype") or "") == "sfc"
        and str(row.get("param") or "").lower() == "2t"
        and str(row.get("step") or "") == str(forecast_hour)
    ]
    if len(matches) != 1:
        raise ContractError("native_forward_probe_aifs_2t_cardinality_invalid")
    row = matches[0]
    if not isinstance(row.get("_offset"), int) or int(row["_offset"]) < 0:
        raise ContractError("native_forward_probe_aifs_range_invalid")
    return content_sha256(row)


def _object_receipt(
    required: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    semantic_status: str,
) -> dict[str, Any]:
    identity = {
        **dict(required),
        "http_status": int(response["http_status"]),
        "requested_at_utc": str(response["requested_at_utc"]),
        "response_started_at_utc": str(response["response_started_at_utc"]),
        "response_completed_at_utc": str(response["response_completed_at_utc"]),
        "response_headers": dict(response.get("response_headers") or {}),
        "response_bytes": int(response.get("response_bytes") or 0),
        "raw_object_sha256": str(response["raw_object_sha256"]),
        "semantic_status": semantic_status,
    }
    return {**identity, "receipt_content_sha256": content_sha256(identity)}


def execute_native_probe(
    plan: Mapping[str, Any],
    *,
    requester: Callable[..., Mapping[str, Any]] = strict_proxy_request,
    proxy: str = DEFAULT_PROXY,
) -> dict[str, Any]:
    """Execute a plan and classify every product without silent fallback."""

    if plan.get("schema") != SCHEMA or plan.get("content_sha256") != content_sha256(
        {key: value for key, value in plan.items() if key != "content_sha256"}
    ):
        raise ContractError("native_forward_probe_plan_invalid")
    checked_lower_bound = _utc(plan["checked_at_utc"], "checked_at_utc")
    results: list[dict[str, Any]] = []
    retained_raw_objects: list[dict[str, Any]] = []
    request_count = 0
    for product in plan["products"]:
        model = str(product["upstream_model_id"])
        receipts: list[dict[str, Any]] = []
        indeterminate = False
        semantic_invalid = False
        available_count = 0
        missing_count = 0
        for required in product["required_objects"]:
            request_count += 1
            if request_count > MAX_REQUESTS:
                raise ContractError("native_forward_probe_request_cap_exceeded")
            max_bytes = MAX_AIFS_INDEX_BYTES if model == "AIFS" else MAX_ICON_HEAD_BYTES
            try:
                response = dict(
                    requester(
                        required["method"],
                        required["probe_url"],
                        proxy=proxy,
                        max_bytes=max_bytes,
                    )
                )
                raw_body = bytes(response.pop("_raw_body", b""))
                if raw_body:
                    retained_raw_objects.append(
                        {
                            "upstream_model_id": model,
                            "object_url": str(required["object_url"]),
                            "probe_url": str(required["probe_url"]),
                            "valid_time_utc": str(required["valid_time_utc"]),
                            "raw_object_sha256": str(response["raw_object_sha256"]),
                            "raw_body": raw_body,
                        }
                    )
                status = int(response["http_status"])
                if status == 200:
                    try:
                        semantic = (
                            "AIFS_2T_INDEX_MATCH:"
                            + _aifs_semantic(raw_body, int(required["forecast_hour"]))
                            if model == "AIFS"
                            else "ICON_T_2M_OBJECT_PRESENT"
                        )
                        available_count += 1
                    except ContractError as exc:
                        semantic = "INVALID:" + str(exc)
                        semantic_invalid = True
                elif status in {404, 410}:
                    semantic = "TERMINAL_MISSING"
                    missing_count += 1
                else:
                    semantic = "INDETERMINATE_HTTP"
                    indeterminate = True
                receipts.append(_object_receipt(required, response, semantic_status=semantic))
            except ContractError as exc:
                indeterminate = True
                now = _iso(datetime.now(timezone.utc))
                synthetic = {
                    "http_status": 0,
                    "requested_at_utc": now,
                    "response_started_at_utc": now,
                    "response_completed_at_utc": now,
                    "response_headers": {},
                    "response_bytes": 0,
                    "raw_object_sha256": hashlib.sha256(b"").hexdigest(),
                }
                receipts.append(
                    _object_receipt(
                        required,
                        synthetic,
                        semantic_status="INDETERMINATE:" + str(exc),
                    )
                )
        required_count = int(product["required_object_count"])
        if indeterminate:
            terminal = "INDETERMINATE"
        elif available_count == required_count and not semantic_invalid:
            terminal = "NATIVE_COMPLETE_NO_FALLBACK"
        elif semantic_invalid:
            terminal = "NATIVE_INVALID"
        elif missing_count == required_count:
            terminal = "NATIVE_UNAVAILABLE"
        elif datetime.now(timezone.utc) >= _utc(
            product["wait_deadline_utc"], "wait_deadline_utc"
        ):
            terminal = "NATIVE_LATE"
        else:
            terminal = "INDETERMINATE"
        receipt_set_sha = content_sha256(receipts)
        results.append(
            {
                **{key: value for key, value in product.items() if key != "required_objects"},
                "terminal_status": terminal,
                "available_object_count": available_count,
                "missing_object_count": missing_count,
                "object_receipts": receipts,
                "terminal_object_receipt_set_sha256": receipt_set_sha,
                "native_unavailability_receipt": None,
            }
        )
    checked = max(checked_lower_bound, datetime.now(timezone.utc))
    checked_text = _iso(checked)
    expires = _iso(checked + timedelta(seconds=NATIVE_RECEIPT_TTL_SECONDS))
    for row in results:
        if row["terminal_status"] not in TERMINAL_FALLBACK_STATUSES:
            continue
        identity = {
            "upstream_model_id": row["upstream_model_id"],
            "station_id": plan["station_id"],
            "event_date": plan["event_date"],
            "settlement_timezone": plan["settlement_timezone"],
            "station_scope_sha256": plan["station_scope_sha256"],
            "native_provider_id": row["native_provider_id"],
            "native_product_id": row["native_product_id"],
            "candidate_cycle_policy_sha256": row["candidate_cycle_policy_sha256"],
            "wait_deadline_policy_sha256": row["wait_deadline_policy_sha256"],
            "checked_at_utc": checked_text,
            "expires_at_utc": expires,
            "terminal_status": row["terminal_status"],
            "required_object_count": row["required_object_count"],
            "terminal_object_receipt_set_sha256": row[
                "terminal_object_receipt_set_sha256"
            ],
        }
        row["native_unavailability_receipt"] = {
            **identity,
            "receipt_content_sha256": content_sha256(identity),
        }
    body = {
        "schema": "republisher_native_forward_probe_result_v1",
        "plan_content_sha256": plan["content_sha256"],
        "station_id": plan["station_id"],
        "event_date": plan["event_date"],
        "settlement_timezone": plan["settlement_timezone"],
        "checked_at_utc": checked_text,
        "product_results": results,
        "fallback_terminal_for_all_products": all(
            row["terminal_status"] in TERMINAL_FALLBACK_STATUSES for row in results
        ),
        "native_complete_for_any_product": any(
            row["terminal_status"] == "NATIVE_COMPLETE_NO_FALLBACK" for row in results
        ),
        "indeterminate_for_any_product": any(
            row["terminal_status"] == "INDETERMINATE" for row in results
        ),
        "proxy_policy": "REQUIRED_127_0_0_1_1082_NO_DIRECT_FALLBACK",
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {
        **body,
        "content_sha256": content_sha256(body),
        "_retained_raw_objects": retained_raw_objects,
    }


def open_meteo_request_url(
    *,
    model: str,
    latitude: float,
    longitude: float,
    event_date: str,
    settlement_timezone: str,
) -> str:
    if model not in {"AIFS", "ICON"} or not all(
        math.isfinite(value) for value in (latitude, longitude)
    ):
        raise ContractError("republisher_forward_request_scope_invalid")
    api_model = {
        "AIFS": "ecmwf_aifs025_single",
        "ICON": "dwd_icon_global",
    }[model]
    query = urlencode(
        {
            "latitude": format(latitude, ".6f"),
            "longitude": format(longitude, ".6f"),
            "hourly": "temperature_2m",
            "models": api_model,
            "start_date": event_date,
            "end_date": event_date,
            "timezone": settlement_timezone,
        }
    )
    return "https://api.open-meteo.com/v1/forecast?" + query


def capture_open_meteo_component(
    *,
    model: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    requester: Callable[..., Mapping[str, Any]] = strict_proxy_request,
    proxy: str = DEFAULT_PROXY,
    max_bytes: int = 4 * 1024 * 1024,
) -> dict[str, Any]:
    """Capture and decode one exact deterministic Open-Meteo station-day."""

    coordinates = station_coordinates(station_id)
    url = open_meteo_request_url(
        model=model,
        latitude=float(coordinates["latitude"]),
        longitude=float(coordinates["longitude"]),
        event_date=event_date,
        settlement_timezone=settlement_timezone,
    )
    response = dict(
        requester("GET", url, proxy=proxy, max_bytes=max_bytes)
    )
    raw_body = bytes(response.pop("_raw_body", b""))
    if int(response.get("http_status") or 0) != 200:
        raise ContractError("republisher_forward_http_not_200")
    if hashlib.sha256(raw_body).hexdigest() != response.get("raw_object_sha256"):
        raise ContractError("republisher_forward_raw_hash_mismatch")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_forward_response_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ContractError("republisher_forward_response_mapping_required")
    if str(payload.get("timezone") or "") != settlement_timezone:
        raise ContractError("republisher_forward_response_timezone_mismatch")
    try:
        response_latitude = float(payload["latitude"])
        response_longitude = float(payload["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("republisher_forward_response_coordinates_invalid") from exc
    if (
        not math.isfinite(response_latitude)
        or not math.isfinite(response_longitude)
        or abs(response_latitude - float(coordinates["latitude"])) > 1.0
        or abs(response_longitude - float(coordinates["longitude"])) > 1.0
    ):
        raise ContractError("republisher_forward_response_coordinates_mismatch")
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ContractError("republisher_forward_hourly_mapping_required")
    times = hourly.get("time")
    values = hourly.get("temperature_2m")
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
        or len(times) not in {23, 24, 25}
    ):
        raise ContractError("republisher_forward_hourly_cohort_invalid")
    curve: list[dict[str, Any]] = []
    for ordinal, (time_value, temperature_value) in enumerate(zip(times, values)):
        try:
            local_time = datetime.fromisoformat(str(time_value))
            temperature = float(temperature_value)
        except (TypeError, ValueError) as exc:
            raise ContractError("republisher_forward_hourly_value_invalid") from exc
        if local_time.date().isoformat() != event_date or not math.isfinite(temperature):
            raise ContractError("republisher_forward_hourly_scope_mismatch")
        curve.append(
            {
                "ordinal": ordinal,
                "local_valid_time": local_time.isoformat(timespec="minutes"),
                "temperature_2m_c": format(temperature, ".12f"),
            }
        )
    if [row["local_valid_time"] for row in curve] != sorted(
        {row["local_valid_time"] for row in curve}
    ):
        raise ContractError("republisher_forward_hourly_time_order_invalid")
    product = {
        "AIFS": ("ecmwf_aifs025_single", "ECMWF"),
        "ICON": ("dwd_icon_global", "DWD"),
    }.get(model)
    if product is None:
        raise ContractError("republisher_forward_model_invalid")
    normalized = {
        "upstream_model_id": model,
        "station_id": str(station_id).upper(),
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "republisher_product_id": product[0],
        "hourly_curve": curve,
    }
    identity = {
        "upstream_model_id": model,
        "station_id": str(station_id).upper(),
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "station_coordinate_registry_sha256": str(coordinates["registry_sha256"]),
        "republisher_product_id": product[0],
        "independence_family_id": product[1],
        "request_url": url,
        "http_status": 200,
        "requested_at_utc": str(response["requested_at_utc"]),
        "response_started_at_utc": str(response["response_started_at_utc"]),
        "response_completed_at_utc": str(response["response_completed_at_utc"]),
        "raw_object_sha256": str(response["raw_object_sha256"]),
        "requested_latitude": format(float(coordinates["latitude"]), ".12f"),
        "requested_longitude": format(float(coordinates["longitude"]), ".12f"),
        "response_latitude": format(response_latitude, ".12f"),
        "response_longitude": format(response_longitude, ".12f"),
        "hourly_valid_time_count": len(curve),
        "hourly_curve_content_sha256": content_sha256(curve),
        "normalized_content_sha256": content_sha256(normalized),
        "forecast_daily_max_c": format(
            max(float(row["temperature_2m_c"]) for row in curve), ".12f"
        ),
    }
    receipt = {**identity, "receipt_content_sha256": content_sha256(identity)}
    return {
        "component_receipt": receipt,
        "normalized_curve": normalized,
        "raw_body": raw_body,
    }


__all__ = [
    "DEFAULT_PROXY",
    "PRODUCTS",
    "SCHEMA",
    "TERMINAL_FALLBACK_STATUSES",
    "build_native_probe_plan",
    "capture_open_meteo_component",
    "execute_native_probe",
    "open_meteo_request_url",
    "strict_proxy_request",
]
