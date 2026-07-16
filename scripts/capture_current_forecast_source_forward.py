#!/usr/bin/env python3
"""Capture current forecast curves into immutable research envelopes.

Numerical-model and Weather.com captures are deliberately fetched independently
of the production forecast cache.  Their own observed request/start/complete
timestamps and raw provider payloads are retained for future strict-as-of
scoring; a batch clock never substitutes for a provider transport receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"

import scan_weather_market as swm
from scripts.forecast_source_forward_snapshot import write_forward_metrics_snapshot
from open_meteo_rate_limit_runtime import record_rate_limit, request_gate


OPEN_METEO_FORECAST_API = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_RATE_LIMIT_STATE = ROOT / "data" / "runtime" / "multimodel_sichuan_v31" / "open_meteo_rate_limit.json"
OPEN_METEO_MODELS: tuple[tuple[str, str], ...] = (
    ("ecmwf_model_peak", "ecmwf_ifs025"),
    ("gfs_model_peak", "gfs_seamless"),
    ("icon_model_peak", "icon_global"),
)
_CAPTURE_EVIDENCE_FIELDS = frozenset(
    {
        "requested_at",
        "response_started_at",
        "response_completed_at",
        "capture_time_provenance",
        "response_headers_observed",
        "raw_provider_response_utf8",
    }
)
_SENSITIVE_RAW_RESPONSE_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)\s*(?:\"\s*)?[:=]"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish the latest operational health report without a partial JSON window."""

    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _transport_capture_kwargs(metrics: Mapping[str, Any], *, fallback_observed_at: str) -> dict[str, str]:
    """Return Tier-A evidence only when all raw transport facts are present.

    A batch-level clock is useful health metadata but is not strict-as-of
    evidence.  This keeps a partial vendor response at Tier C rather than
    accidentally upgrading it just because the collector itself ran.
    """

    requested_at = str(metrics.get("requested_at") or "").strip()
    response_started_at = str(metrics.get("response_started_at") or "").strip()
    response_completed_at = str(metrics.get("response_completed_at") or "").strip()
    raw = str(metrics.get("raw_provider_response_utf8") or "")
    provenance = str(metrics.get("capture_time_provenance") or "").strip()
    response_headers_observed = metrics.get("response_headers_observed") is True
    raw_is_safe = bool(raw) and _SENSITIVE_RAW_RESPONSE_PATTERN.search(raw) is None
    observed = bool(
        raw
        and raw_is_safe
        and requested_at
        and response_started_at
        and response_completed_at
        and provenance == "observed_transport"
        and response_headers_observed
    )
    if observed:
        return {
            "requested_at": requested_at,
            "response_started_at": response_started_at,
            "response_completed_at": response_completed_at,
            "capture_time_provenance": "observed_transport",
            "response_headers_observed": True,
            "raw_provider_response_utf8": raw,
            "observed_at": response_completed_at,
        }
    return {
        "requested_at": fallback_observed_at,
        "response_started_at": fallback_observed_at,
        "response_completed_at": fallback_observed_at,
        "capture_time_provenance": "batch_clock_proxy",
        "response_headers_observed": False,
        "raw_provider_response_utf8": "",
        "observed_at": fallback_observed_at,
    }


def _metrics_without_capture_evidence(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in dict(metrics).items()
        if str(key) not in _CAPTURE_EVIDENCE_FIELDS
    }


def _fetch_open_meteo_model_curve(
    *,
    source_id: str,
    model: str,
    event_date: str,
    latitude: float,
    longitude: float,
    timeout_seconds: float = 25.0,
) -> dict[str, Any]:
    """Fetch and validate a one-day live numerical-model temperature curve."""

    parameters = {
        "latitude": f"{float(latitude):.6f}",
        "longitude": f"{float(longitude):.6f}",
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "Asia/Shanghai",
        "start_date": str(event_date)[:10],
        "end_date": str(event_date)[:10],
    }
    request_url = f"{OPEN_METEO_FORECAST_API}?{urlencode(parameters)}"
    requested_at = _utc_now()
    gate = request_gate(state_path=OPEN_METEO_RATE_LIMIT_STATE, now=requested_at)
    if gate.get("ok") is not True:
        return {
            "ok": False,
            "error": str(gate.get("reason") or "open_meteo_rate_limit_gate_blocked"),
            "source_url": request_url,
            "model": model,
            "requested_at": requested_at,
            "availability": gate,
        }
    response_started_at = ""
    try:
        with urlopen(request_url, timeout=float(timeout_seconds)) as response:
            # urlopen returns after response headers; capture the receipt
            # before body consumption so Tier-A timing is factual.
            response_started_at = _utc_now()
            body = response.read().decode("utf-8")
        response_completed_at = _utc_now()
    except HTTPError as exc:
        # An HTTP response is a real transport receipt.  Preserve only its
        # status/deadline facts here; a failed provider response is never a
        # forecast snapshot or a zero-score observation.
        response_started_at = _utc_now()
        try:
            response_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            response_body = ""
        response_completed_at = _utc_now()
        status = int(exc.code)
        retry_after = getattr(exc, "headers", {}).get("Retry-After")
        availability: dict[str, Any] = {
            "schema": "forecast_source_provider_availability_v1",
            "http_status": status,
            "requested_at": requested_at,
            "response_started_at": response_started_at,
            "response_completed_at": response_completed_at,
            "response_headers_observed": True,
            "provider_state": "rate_limited" if status == 429 else "http_error",
        }
        if status == 429:
            cooldown = record_rate_limit(
                state_path=OPEN_METEO_RATE_LIMIT_STATE,
                observed_at=response_completed_at,
                source=f"forecast_source_forward:{source_id}",
                retry_after_seconds=retry_after,
                http_status=status,
            )
            availability["retry_not_before"] = cooldown["retry_not_before"]
        return {
            "ok": False,
            "error": f"open_meteo_http_{status}",
            "source_url": request_url,
            "model": model,
            "requested_at": requested_at,
            "response_started_at": response_started_at,
            "response_completed_at": response_completed_at,
            "availability": availability,
            # Never expose a provider-error body through the latest health
            # report; it is neither forecast evidence nor needed for a live
            # decision. The shared cooldown is the durable receipt.
            "provider_error_body_present": bool(response_body),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"open_meteo_fetch_failed:{type(exc).__name__}",
            "source_url": request_url,
            "model": model,
            "requested_at": requested_at,
            "response_started_at": response_started_at,
        }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "open_meteo_response_json_invalid",
            "source_url": request_url,
            "model": model,
            "requested_at": requested_at,
            "response_started_at": response_started_at,
            "response_completed_at": response_completed_at,
        }
    hourly = payload.get("hourly") if isinstance(payload, Mapping) else None
    times = hourly.get("time") if isinstance(hourly, Mapping) and isinstance(hourly.get("time"), list) else []
    temperatures = hourly.get("temperature_2m") if isinstance(hourly, Mapping) and isinstance(hourly.get("temperature_2m"), list) else []
    if len(times) != len(temperatures):
        return {
            "ok": False,
            "error": "open_meteo_hourly_length_mismatch",
            "source_url": request_url,
            "model": model,
        }
    curve: list[dict[str, Any]] = []
    for valid_time, temperature in zip(times, temperatures):
        try:
            value = float(temperature)
        except (TypeError, ValueError):
            continue
        if str(valid_time)[:10] == str(event_date)[:10]:
            curve.append({"valid_time_local": str(valid_time), "temperature_c": value})
    if not curve:
        return {
            "ok": False,
            "error": "open_meteo_target_day_curve_missing",
            "source_url": request_url,
            "model": model,
        }
    peak = max(row["temperature_c"] for row in curve)
    return {
        "ok": True,
        "source_id": source_id,
        "model": model,
        "value_c": peak,
        "forecast_peak_c": peak,
        "hourly_curve": curve,
        "source_url": request_url,
        "requested_at": requested_at,
        "response_started_at": response_started_at,
        "response_completed_at": response_completed_at,
        "capture_time_provenance": "observed_transport",
        "response_headers_observed": True,
        "raw_provider_response_utf8": body,
    }


def _fetch_wunderground_inline_forward_curve(
    *,
    resolution_source: str,
    event_date: str,
    station_id: str,
) -> dict[str, Any]:
    """Fetch the public WU hourly page as one observed raw forecast response.

    The page's embedded hourly payload is preferred to its separately exposed
    Weather.com API URL: that API can reject the collector even while the
    public page visibly contains the forecast.  A credential-shaped page body
    is retained only as a non-Tier-A fallback by the caller; this helper never
    claims that a batch timestamp is the page's receipt time.
    """

    source_url = str(swm._wunderground_forecast_hourly_source(resolution_source) or "").strip()
    if not source_url:
        return {"ok": False, "error": "wu_hourly_source_missing"}
    requested_at = _utc_now()
    response_started_at = ""
    try:
        response = swm.requests_get(
            source_url,
            headers=swm.DEFAULT_REQUEST_HEADERS,
            timeout=swm._wu_forecast_request_timeout(),
            bypass_proxy=True,
            retries=swm._wu_forecast_request_retries(),
            stream=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"wu_hourly_page_fetch_failed:{type(exc).__name__}"}
    # `stream=True` returns when response headers arrive.  Take the strict
    # as-of receipt before body consumption, then close the owned transport.
    response_started_at = _utc_now()
    raw_body = str(getattr(response, "text", "") or "")
    response_completed_at = _utc_now()
    swm.close_streaming_requests_response(response)
    if getattr(response, "status_code", None) != 200:
        return {"ok": False, "error": f"wu_hourly_page_http_{getattr(response, 'status_code', 'unknown')}"}
    if not raw_body or _SENSITIVE_RAW_RESPONSE_PATTERN.search(raw_body):
        return {"ok": False, "error": "wu_hourly_page_raw_body_not_safe_for_immutable_capture"}
    hourly_api_url = swm._extract_wunderground_hourly_api_url(raw_body)
    rows = swm._extract_wunderground_inline_hourly_rows(
        raw_body,
        hourly_api_url=hourly_api_url,
    )
    event_key = str(event_date or "")[:10]
    curve: list[dict[str, Any]] = []
    for row in rows:
        valid_time_local = str(row.get("valid_time_local") or "")
        if not valid_time_local.startswith(event_key):
            continue
        raw_temperature = row.get("temperature")
        try:
            value_c = float(
                swm._convert_temp_value(
                    float(raw_temperature),
                    str(row.get("unit") or "F"),
                    "C",
                )
            )
        except (TypeError, ValueError):
            continue
        curve.append({"valid_time_local": valid_time_local, "temperature_c": value_c})
    if not curve:
        return {"ok": False, "error": "wu_hourly_page_target_curve_missing"}
    curve.sort(key=lambda item: str(item["valid_time_local"]))
    peak_c = max(float(item["temperature_c"]) for item in curve)
    return {
        "ok": True,
        "source_id": "wu_forecast_peak",
        "source": "wunderground_hourly_page_inline_forecast",
        "source_url": source_url,
        "station_id": station_id,
        "event_date": event_key,
        "forecast_peak_c": peak_c,
        "value_c": peak_c,
        "hourly_curve": curve,
        "requested_at": requested_at,
        "response_started_at": response_started_at,
        "response_completed_at": response_completed_at,
        "capture_time_provenance": "observed_transport",
        "response_headers_observed": True,
        "raw_provider_response_utf8": raw_body,
    }


def capture_current_forward_sources(
    *,
    event_date: str,
    station_id: str = "ZUUU",
    resolution_source: str = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    latitude: float = 30.5728,
    longitude: float = 104.0668,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    observed_at = _utc_now()
    results: dict[str, Any] = {}
    for source_id, model in OPEN_METEO_MODELS:
        metrics = _fetch_open_meteo_model_curve(
            source_id=source_id,
            model=model,
            event_date=event_date,
            latitude=latitude,
            longitude=longitude,
        )
        if metrics.get("ok") is True and metrics.get("value_c") is not None:
            results[source_id] = write_forward_metrics_snapshot(
                source_id=source_id,
                target_event_date_local=event_date,
                station_id=station_id,
                metrics=metrics,
                observed_at=observed_at,
                requested_at=str(metrics.get("requested_at") or ""),
                response_started_at=str(metrics.get("response_started_at") or ""),
                response_completed_at=str(metrics.get("response_completed_at") or ""),
                capture_time_provenance=str(metrics.get("capture_time_provenance") or "unknown"),
                response_headers_observed=metrics.get("response_headers_observed") is True,
                raw_provider_response_utf8=str(metrics.get("raw_provider_response_utf8") or ""),
                allow_research_write=allow_research_write,
            )
        else:
            results[source_id] = {
                "ok": False,
                "state": "unavailable",
                "state_reason": str(metrics.get("error") or "open_meteo_forecast_unavailable"),
                "availability": metrics.get("availability") if isinstance(metrics.get("availability"), Mapping) else {},
            }
    try:
        wu = _fetch_wunderground_inline_forward_curve(
            resolution_source=resolution_source,
            event_date=event_date,
            station_id=station_id,
        )
    except Exception as exc:
        wu = {"ok": False, "error": f"wu_inline_fetch_failed:{type(exc).__name__}"}
    if not isinstance(wu, Mapping) or wu.get("ok") is not True or wu.get("value_c") is None:
        # Preserve prior forward-only visibility when the direct raw-page path
        # is unavailable.  The fallback never carries raw transport evidence,
        # so it remains Tier C rather than silently upgrading historical skill.
        try:
            wu = swm.fetch_wunderground_forecast_peak_metrics(
                resolution_source,
                event_date,
                "temperature_2m_max",
                "C",
            )
        except Exception as exc:
            wu = {"ok": False, "error": f"wu_fetch_failed:{type(exc).__name__}"}
    if isinstance(wu, Mapping) and wu.get("ok") is True and wu.get("value_c") is not None:
        transport = _transport_capture_kwargs(wu, fallback_observed_at=observed_at)
        results["wu_forecast_peak"] = write_forward_metrics_snapshot(
            source_id="wu_forecast_peak",
            target_event_date_local=event_date,
            station_id=station_id,
            metrics=_metrics_without_capture_evidence(wu),
            observed_at=transport["observed_at"],
            requested_at=transport["requested_at"],
            response_started_at=transport["response_started_at"],
            response_completed_at=transport["response_completed_at"],
            capture_time_provenance=transport["capture_time_provenance"],
            response_headers_observed=transport["response_headers_observed"] is True,
            raw_provider_response_utf8=transport["raw_provider_response_utf8"],
            allow_research_write=allow_research_write,
        )
    else:
        results["wu_forecast_peak"] = {"ok": False, "state": "unavailable", "state_reason": str((wu or {}).get("error") or "wu_forecast_unavailable")}
    try:
        twc_hourly = swm.fetch_weather_com_hourly_peak_metrics(
            latitude,
            longitude,
            event_date,
            "temperature_2m_max",
            "C",
            station_id=station_id,
            resolution_source=resolution_source,
        )
    except Exception as exc:
        twc_hourly = {"ok": False, "error": f"twc_hourly_fetch_failed:{type(exc).__name__}"}
    try:
        twc_daily = swm.fetch_weather_com_forecast_peak_metrics(
            latitude,
            longitude,
            event_date,
            "temperature_2m_max",
            "C",
            station_id=station_id,
            resolution_source=resolution_source,
            include_capture_evidence=True,
        )
    except Exception as exc:
        twc_daily = {"ok": False, "error": f"twc_daily_fetch_failed:{type(exc).__name__}"}
    if isinstance(twc_daily, Mapping) and twc_daily.get("ok") is True and twc_daily.get("value_c") is not None:
        transport = _transport_capture_kwargs(twc_daily, fallback_observed_at=observed_at)
        metrics = _metrics_without_capture_evidence(twc_daily)
        metrics["forecast_peak_c"] = float(twc_daily["value_c"])
        # Weather.com daily's maximum is a target-day scalar.  Its provider
        # record time is not a claimed peak time, and a separate hourly
        # response may legitimately disagree with that daily product.
        metrics["forecast_value_kind"] = "daily_max_event_window"
        metrics["forecast_record_valid_time"] = str(twc_daily.get("forecast_valid_local") or "")
        metrics["trajectory_eligible"] = False
        if isinstance(twc_hourly, Mapping) and isinstance(twc_hourly.get("hourly_curve"), list):
            metrics["auxiliary_hourly_curve"] = list(twc_hourly["hourly_curve"])
        results["weather_com_forecast_peak"] = write_forward_metrics_snapshot(
            source_id="weather_com_forecast_peak",
            target_event_date_local=event_date,
            station_id=station_id,
            metrics=metrics,
            observed_at=transport["observed_at"],
            requested_at=transport["requested_at"],
            response_started_at=transport["response_started_at"],
            response_completed_at=transport["response_completed_at"],
            capture_time_provenance=transport["capture_time_provenance"],
            response_headers_observed=transport["response_headers_observed"] is True,
            raw_provider_response_utf8=transport["raw_provider_response_utf8"],
            allow_research_write=allow_research_write,
        )
    else:
        results["weather_com_forecast_peak"] = {"ok": False, "state": "unavailable", "state_reason": str((twc_daily or {}).get("error") or "twc_forecast_unavailable")}
    return {
        "schema": "current_forecast_source_forward_capture_v1",
        "source_data_policy": "external_only",
        "event_date": event_date,
        "station_id": station_id,
        "observed_at": observed_at,
        "results": results,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--resolution-source", default="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU")
    parser.add_argument("--latitude", type=float, default=30.5728)
    parser.add_argument("--longitude", type=float, default=104.0668)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = capture_current_forward_sources(
        event_date=args.event_date,
        station_id=args.station_id,
        resolution_source=args.resolution_source,
        latitude=args.latitude,
        longitude=args.longitude,
        allow_research_write=args.allow_research_write,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("output_requires_allow_research_write")
        output = args.output.expanduser().resolve()
        if output != RESEARCH_ROOT.resolve() and RESEARCH_ROOT.resolve() not in output.parents:
            raise ValueError("forward_capture_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomically(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
