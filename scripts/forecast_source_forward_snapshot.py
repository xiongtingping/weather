#!/usr/bin/env python3
"""Wrap an already-fetched forecast response in an immutable research envelope.

The command is dry-run by default.  Writing a gzip envelope requires
``--allow-research-write`` and is restricted to the dedicated forecast source
skill research root.  It never fetches data itself and never writes facts.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from external_forecast_snapshot_runtime import (
    build_external_forecast_raw_envelope,
    canonical_envelope_json,
    default_provider_family,
    redact_request_url,
    validate_external_forecast_raw_envelope,
)


DEFAULT_RESEARCH_RAW_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "raw"
FORWARD_METRICS_SCHEMA = "forecast_source_forward_metrics_v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    allowed = DEFAULT_RESEARCH_RAW_ROOT.resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ValueError("forward_snapshot_output_outside_research_raw_root")
    return resolved


def _relative_output_path(envelope: dict[str, Any]) -> Path:
    completed = str(envelope.get("response_completed_at") or "")
    try:
        parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    source = str(envelope.get("source_id") or "unknown").replace("/", "_")
    digest = str(envelope.get("response_sha256") or "")[:16]
    filename = parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"_{digest}.json.gz"
    return Path(source) / parsed.strftime("%Y") / parsed.strftime("%m") / parsed.strftime("%d") / filename


def write_forward_snapshot(
    envelope: dict[str, Any],
    *,
    output_root: Path = DEFAULT_RESEARCH_RAW_ROOT,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    contract = validate_external_forecast_raw_envelope(envelope)
    if not contract["ok"]:
        return {
            "ok": False,
            "state": "blocked",
            "state_reason": "external_forecast_envelope_contract_failed",
            "contract": contract,
            "writes_research_files": False,
        }
    relative = _relative_output_path(envelope)
    output = _safe_root(output_root / relative)
    if not allow_research_write:
        return {
            "ok": True,
            "state": "dry_run",
            "planned_output_path": str(output),
            "contract": contract,
            "writes_research_files": False,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_envelope_json(envelope)
    if output.exists():
        with gzip.open(output, "rt", encoding="utf-8") as handle:
            existing = json.load(handle)
        if str(existing.get("response_sha256") or "") != str(envelope.get("response_sha256") or ""):
            raise ValueError("forward_snapshot_path_hash_collision")
        state = "already_present"
    else:
        temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
        try:
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        state = "written"
    return {
        "ok": True,
        "state": state,
        "output_path": str(output),
        "contract": contract,
        "writes_research_files": state == "written",
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
        },
    }


def _sanitize_metrics(value: Any) -> Any:
    sensitive = {"apikey", "api_key", "api-key", "token", "authorization", "password", "secret", "key"}
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in sensitive:
                continue
            output[str(key)] = redact_request_url(item) if "url" in normalized and isinstance(item, str) else _sanitize_metrics(item)
        return output
    if isinstance(value, list):
        return [_sanitize_metrics(item) for item in value]
    return value


def write_forward_metrics_snapshot(
    *,
    source_id: str,
    target_event_date_local: str,
    station_id: str,
    metrics: dict[str, Any],
    observed_at: str,
    requested_at: str = "",
    response_started_at: str = "",
    response_completed_at: str = "",
    capture_time_provenance: str = "unknown",
    response_headers_observed: bool = False,
    raw_provider_response_utf8: str = "",
    output_root: Path = DEFAULT_RESEARCH_RAW_ROOT,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    """Archive one derived live forecast, deduping unchanged source/event bodies."""

    cleaned = _sanitize_metrics(dict(metrics))
    peak = cleaned.get("forecast_peak_c")
    if peak is None:
        peak = cleaned.get("value_c")
    payload = {
        "schema": FORWARD_METRICS_SCHEMA,
        "source_id": str(source_id or ""),
        "provider_family": default_provider_family(source_id),
        "station_id": str(station_id or ""),
        "target_event_date_local": str(target_event_date_local or "")[:10],
        "forecast_peak_c": peak,
        "temperature_unit": "C",
        "hourly_curve": cleaned.get("hourly_curve") if isinstance(cleaned.get("hourly_curve"), list) else [],
        "metrics": cleaned,
        "capture_time_provenance": str(capture_time_provenance or "unknown"),
        "response_headers_observed": response_headers_observed is True,
    }
    # A daily maximum is valid for an event-day window, not necessarily at a
    # particular hourly peak.  Preserve the provider's own semantics instead
    # of turning a day anchor into a fabricated temperature trajectory point.
    for key in (
        "forecast_value_kind",
        "forecast_record_valid_time",
        "forecast_peak_valid_time",
        "trajectory_eligible",
    ):
        if key in cleaned:
            payload[key] = cleaned[key]
    required = (payload["source_id"], payload["station_id"], payload["target_event_date_local"], observed_at)
    if not all(required) or peak is None:
        return {
            "ok": False,
            "state": "unavailable",
            "state_reason": "forward_metrics_contract_incomplete",
            "writes_research_files": False,
        }
    raw_response = str(raw_provider_response_utf8 or "")
    body_payload: dict[str, Any] = payload
    if raw_response:
        body_payload = {
            "schema": "forecast_source_forward_capture_payload_v2",
            "derived_forecast": payload,
            "raw_provider_response_utf8": raw_response,
        }
    body = json.dumps(body_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_url = str(cleaned.get("source_url") or cleaned.get("forecast_source_url") or cleaned.get("hourly_forecast_source_url") or "")
    requested = str(requested_at or observed_at)
    started = str(response_started_at or requested)
    completed = str(response_completed_at or observed_at or started)
    envelope = build_external_forecast_raw_envelope(
        source_id=payload["source_id"],
        provider_family=payload["provider_family"],
        response_body_utf8=body,
        requested_at=requested,
        response_started_at=started,
        response_completed_at=completed,
        provider_available_at=completed,
        http_status=200,
        parser_version="forecast_source_forward_metrics_v2",
        request_url=request_url,
        request_parameters={
            "target_event_date_local": payload["target_event_date_local"],
            "station_id": payload["station_id"],
        },
        source_event_id=f"forward_{payload['source_id']}_{payload['target_event_date_local']}",
        availability_mode="live_fetch",
        provider_available_at_quality="observed_response_completion",
        capture_time_provenance=str(capture_time_provenance or "unknown"),
        response_headers_observed=response_headers_observed is True,
    )
    safe_root = _safe_root(output_root)
    index_path = safe_root / "_latest_derived" / payload["source_id"] / f"{payload['target_event_date_local']}.json"
    if index_path.exists():
        try:
            prior = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        if str(prior.get("response_sha256") or "") == str(envelope.get("response_sha256") or ""):
            return {
                "ok": True,
                "state": "already_current",
                "response_sha256": envelope["response_sha256"],
                "writes_research_files": False,
            }
    result = write_forward_snapshot(
        envelope,
        output_root=safe_root,
        allow_research_write=allow_research_write,
    )
    if allow_research_write and result.get("ok") is True and result.get("state") in {"written", "already_present"}:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_payload = {
            "schema": "forecast_source_forward_metrics_latest_pointer_v1",
            "source_id": payload["source_id"],
            "target_event_date_local": payload["target_event_date_local"],
            "response_sha256": envelope["response_sha256"],
            "snapshot_path": result.get("output_path"),
            "as_of_eligible_at": envelope.get("as_of_eligible_at"),
        }
        temporary = index_path.with_name(f".{index_path.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps(index_payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, index_path)
    result["response_sha256"] = envelope["response_sha256"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--provider-family", default="")
    parser.add_argument("--payload-file", type=Path, help="Raw response body file; stdin when omitted.")
    parser.add_argument("--request-url", default="")
    parser.add_argument("--request-parameters-json", default="{}")
    parser.add_argument("--requested-at", default="")
    parser.add_argument("--response-started-at", default="")
    parser.add_argument("--response-completed-at", default="")
    parser.add_argument("--provider-available-at", default="")
    parser.add_argument("--http-status", type=int, default=200)
    parser.add_argument("--parser-version", required=True)
    parser.add_argument("--source-event-id", default="")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RESEARCH_RAW_ROOT)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()

    body = args.payload_file.read_text(encoding="utf-8") if args.payload_file else sys.stdin.read()
    now = _now_iso()
    requested_at = args.requested_at or now
    response_started_at = args.response_started_at or requested_at
    response_completed_at = args.response_completed_at or response_started_at
    request_parameters = json.loads(args.request_parameters_json)
    if not isinstance(request_parameters, dict):
        raise SystemExit("--request-parameters-json must be an object")
    envelope = build_external_forecast_raw_envelope(
        source_id=args.source_id,
        provider_family=args.provider_family,
        response_body_utf8=body,
        requested_at=requested_at,
        response_started_at=response_started_at,
        response_completed_at=response_completed_at,
        provider_available_at=args.provider_available_at or None,
        http_status=args.http_status,
        parser_version=args.parser_version,
        request_url=args.request_url,
        request_parameters=request_parameters,
        source_event_id=args.source_event_id,
    )
    result = write_forward_snapshot(
        envelope,
        output_root=args.output_root,
        allow_research_write=args.allow_research_write,
    )
    print(json.dumps({"envelope": envelope, "result": result}, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
