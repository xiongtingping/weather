#!/usr/bin/env python3
"""Capture five bounded ECMWF IFS 0.25° 2t messages from one official run."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival_capture_config import load_target_capture_config  # noqa: E402
from bayes_basket_target.model_curve_schedule import aligned_target_sample_offsets  # noqa: E402
from bayes_basket_target.model_arrival import validate_prospective_capture_window  # noqa: E402
from bayes_basket_target.model_run_contract import validate_capture_request  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402
from scripts.build_bayes_basket_target_global_model_fanout import fanout  # noqa: E402
from scripts.capture_bayes_basket_target_model_arrival import (  # noqa: E402
    DEFAULT_ROOT as DEFAULT_RAW_ROOT,
    capture,
)
from official_nwp_https_transport import open_official_https, response_read_deadline  # noqa: E402


SCHEMA = "bayes_basket_target_ecmwf_same_run_capture_v1"
PROVIDER = "ecmwf_ifs025_official"
ECMWF_HOST = "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
ECMWF_ROOT = f"https://{ECMWF_HOST}"
MAX_ECMWF_FORECAST_HOUR = 360
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_SCAN = 5000
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_CAPTURE_CONFIG = ROOT / "config" / "bayes_basket_target" / "model_arrival_capture_v1.json"
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "audits" / "bayes_basket_target_ecmwf_same_run"


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("ecmwf_same_run_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("ecmwf_same_run_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_ecmwf_urls(*, run_initialization: datetime, forecast_hour: int) -> tuple[str, str]:
    run = run_initialization.astimezone(timezone.utc)
    run_day = run.strftime("%Y%m%d")
    run_hour = run.strftime("%H")
    stem = f"{run:%Y%m%d%H%M%S}-{int(forecast_hour)}h-oper-fc"
    base = f"{ECMWF_ROOT}/{run_day}/{run_hour}z/ifs/0p25/oper/{stem}"
    return base + ".grib2", base + ".index"


def parse_2t_index_range(
    index_body: bytes,
    *,
    run_initialization: datetime,
    forecast_hour: int,
    max_message_bytes: int,
) -> tuple[int, int, dict[str, Any]]:
    """Select exactly one surface 2t message and return its inclusive range."""

    if len(index_body) > MAX_INDEX_BYTES:
        raise ContractError("ecmwf_same_run_index_too_large")
    run = run_initialization.astimezone(timezone.utc)
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for raw_line in index_body.splitlines():
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("ecmwf_same_run_index_json_invalid") from exc
        if not isinstance(item, Mapping):
            raise ContractError("ecmwf_same_run_index_row_invalid")
        if (
            str(item.get("param") or "").lower() != "2t"
            or str(item.get("levtype") or "").lower() != "sfc"
            or str(item.get("step") or "") != str(int(forecast_hour))
        ):
            continue
        if (
            str(item.get("date") or "") != run.strftime("%Y%m%d")
            or str(item.get("time") or "").zfill(4) != run.strftime("%H%M")
            or str(item.get("type") or "").lower() != "fc"
            or str(item.get("stream") or "").lower() != "oper"
        ):
            raise ContractError("ecmwf_same_run_index_identity_mismatch")
        try:
            start = int(item["_offset"])
            length = int(item["_length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("ecmwf_same_run_index_range_invalid") from exc
        end = start + length - 1
        if start < 0 or length <= 0 or length > int(max_message_bytes) or end < start:
            raise ContractError("ecmwf_same_run_2t_range_invalid")
        matches.append((start, end, dict(item)))
    if len(matches) != 1:
        raise ContractError("ecmwf_same_run_2t_index_cardinality_invalid")
    return matches[0]


def _fetch_index(url: str, *, timeout_sec: float) -> tuple[bytes, dict[str, str]]:
    if not url.startswith(f"https://{ECMWF_HOST}/") or not url.endswith(".index"):
        raise ContractError("ecmwf_same_run_index_url_invalid")
    request = Request(url, headers={"User-Agent": "bayes-basket-target-ecmwf-index/1"})
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    body = bytearray()
    with open_official_https(request, timeout_sec=timeout_sec) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status != 200:
            raise ContractError(f"ecmwf_same_run_index_http_{status}")
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        advertised = headers.get("content-length")
        if advertised:
            try:
                if int(advertised) > MAX_INDEX_BYTES:
                    raise ContractError("ecmwf_same_run_index_too_large")
            except ValueError as exc:
                raise ContractError("ecmwf_same_run_index_content_length_invalid") from exc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ecmwf_same_run_index_timeout")
            with response_read_deadline(remaining):
                chunk = bytes(response.read(64 * 1024) or b"")
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_INDEX_BYTES:
                raise ContractError("ecmwf_same_run_index_too_large")
    if not body:
        raise ContractError("ecmwf_same_run_index_empty")
    return bytes(body), headers


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _find_existing_receipt(
    *,
    raw_root: Path,
    target_local_date: str,
    run_initialization_utc: str,
    valid_time_utc: str,
    target_timezone: str,
    station_id: str,
) -> Path | None:
    receipt_dir = raw_root.expanduser().resolve() / PROVIDER / "receipts"
    paths = sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []
    if len(paths) > MAX_RECEIPT_SCAN:
        raise ContractError("ecmwf_same_run_receipt_scan_cap_exceeded")
    for path in paths:
        try:
            value = load_bounded_json(path)
        except Exception:
            continue
        if not isinstance(value, Mapping) or any((
            value.get("provider_id") != PROVIDER,
            value.get("station_id") != station_id,
            value.get("target_local_date") != target_local_date,
            value.get("run_initialization_utc") != run_initialization_utc,
            value.get("valid_time_utc") != valid_time_utc,
            value.get("capture_mode") != "prospective",
        )):
            continue
        raw_receipts = value.get("raw_receipts")
        if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
            continue
        raw = raw_receipts[0]
        if not isinstance(raw, Mapping):
            continue
        raw_path = Path(str(raw.get("raw_object_ref") or "")).expanduser().resolve()
        expected = str(raw.get("raw_sha256") or "").lower()
        if (
            len(expected) == 64
            and raw_path.is_file()
            and _sha256_file(raw_path) == expected
        ):
            return path.resolve()
    return None


def build_plan(
    *,
    target_local_date: str,
    run_initialization_utc: str,
    as_of_utc: str,
    contract_path: Path,
    capture_config_path: Path = DEFAULT_CAPTURE_CONFIG,
) -> dict[str, Any]:
    run = _parse_utc(run_initialization_utc)
    contract = load_bounded_json(contract_path.expanduser().resolve())
    if not isinstance(contract, Mapping):
        raise ContractError("model_run_contract_mapping_required")
    target_timezone = str(contract.get("target_timezone") or "").strip()
    stations = [
        str(value).strip().upper()
        for value in contract.get("station_ids") or []
        if str(value).strip()
    ]
    if not target_timezone:
        raise ContractError("model_run_contract_timezone_missing")
    if not stations:
        raise ContractError("model_run_contract_station_scope_missing")
    if not str(contract.get("market_registry_snapshot_sha256") or ""):
        raise ContractError("model_run_contract_registry_lineage_missing")
    source_station_id = stations[0]
    capture_window = validate_prospective_capture_window(
        target_local_date=target_local_date,
        target_timezone=target_timezone,
        run_initialization_utc=_stamp(run),
        decision_at_utc=as_of_utc,
    )
    validate_capture_request(
        contract,
        provider_id=PROVIDER,
        station_id=source_station_id,
        target_local_date=target_local_date,
        run_initialization_utc=_stamp(run),
        target_timezone=target_timezone,
    )
    capture_config = load_target_capture_config(capture_config_path)
    budget = dict(capture_config["resource_budget"])
    window = settlement_day_window(target_local_date, target_timezone)
    target_start = _parse_utc(window["target_day_start_utc"])
    if run > target_start:
        raise ContractError("model_curve_run_newer_than_target_sample")
    offsets = aligned_target_sample_offsets(
        target_day_start_utc=target_start,
        run_initialization=run,
    )
    forecast_hours = tuple(
        int(((target_start + timedelta(hours=offset)) - run).total_seconds() // 3600)
        for offset in offsets
    )
    if min(forecast_hours) < 0:
        raise ContractError("model_curve_run_newer_than_target_sample")
    if max(forecast_hours) > MAX_ECMWF_FORECAST_HOUR:
        raise ContractError("model_curve_forecast_hour_exceeds_supported_horizon")
    points: list[dict[str, Any]] = []
    for forecast_hour in forecast_hours:
        grib_url, index_url = build_ecmwf_urls(
            run_initialization=run,
            forecast_hour=forecast_hour,
        )
        points.append({
            "forecast_hour": forecast_hour,
            "valid_time_utc": _stamp(run + timedelta(hours=forecast_hour)),
            "grib_url": grib_url,
            "index_url": index_url,
        })
    identity = {
        "schema": SCHEMA,
        "target_local_date": target_local_date,
        "target_timezone": target_timezone,
        "run_initialization_utc": _stamp(run),
        "as_of_utc": _stamp(_parse_utc(as_of_utc)),
        "capture_window": capture_window,
        "provider_id": PROVIDER,
        "source_station_id": source_station_id,
        "station_ids": stations,
        "points": points,
        "model_run_contract": contract,
        "capture_config": capture_config,
        "resource_budget": budget,
        "boundary": {
            "target_only": True,
            "prospective_only": True,
            "execution_mode": "production_no_live",
            "formal_probability_input_role_eligible": True,
            "p0_probability_input_eligible_now": True,
            "p1_probability_input_eligible_now": capture_window["state"] == "open",
            "raw_capture_may_precede_event": True,
            "p0_consumption_before_event": True,
            "p1_consumption_before_event": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "plan_sha256": content_sha256(identity)}


def execute_plan(
    plan: Mapping[str, Any],
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    identity = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if str(plan.get("plan_sha256") or "") != content_sha256(identity):
        raise ContractError("ecmwf_same_run_plan_sha256_mismatch")
    if plan.get("provider_id") != PROVIDER or plan.get("boundary", {}).get("calls_order_gateway") is not False:
        raise ContractError("ecmwf_same_run_plan_boundary_invalid")
    now = _stamp(datetime.now(timezone.utc))
    window = validate_prospective_capture_window(
        target_local_date=str(plan["target_local_date"]),
        target_timezone=str(plan["target_timezone"]),
        run_initialization_utc=str(plan["run_initialization_utc"]),
        decision_at_utc=now,
    )
    budget = dict(plan["resource_budget"])
    assert_process_memory_budget(limit=int(budget["max_process_rss_bytes"]))
    assert_target_raw_retention_budget(
        raw_root,
        bytes_needed=0,
        limit=int(budget["max_raw_bytes"]),
        min_free_bytes=int(budget["min_free_bytes"]),
    )
    run = str(plan["run_initialization_utc"])
    day = str(plan["target_local_date"])
    destination = (
        output_root.expanduser().resolve()
        / f"{day}_{run.replace('-', '').replace(':', '')}"
    )
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for point in plan["points"]:
        assert_process_memory_budget(limit=int(budget["max_process_rss_bytes"]))
        assert_target_raw_retention_budget(
            raw_root,
            bytes_needed=int(budget["max_response_bytes"]),
            limit=int(budget["max_raw_bytes"]),
            min_free_bytes=int(budget["min_free_bytes"]),
        )
        valid = str(point["valid_time_utc"])
        existing = _find_existing_receipt(
            raw_root=raw_root,
            target_local_date=day,
            run_initialization_utc=run,
            valid_time_utc=valid,
            target_timezone=str(plan["target_timezone"]),
            station_id=str(plan["source_station_id"]),
        )
        if existing is not None:
            records.append({
                "forecast_hour": point["forecast_hour"],
                "valid_time_utc": valid,
                "status": "REUSED",
                "transport_receipt_path": str(existing),
            })
            continue
        try:
            index_body, index_headers = _fetch_index(
                str(point["index_url"]),
                timeout_sec=timeout_sec,
            )
            start, end, index_row = parse_2t_index_range(
                index_body,
                run_initialization=_parse_utc(run),
                forecast_hour=int(point["forecast_hour"]),
                max_message_bytes=int(budget["max_response_bytes"]),
            )
            captured = capture(
                provider_id=PROVIDER,
                urls=[str(point["grib_url"])],
                publication_url=None,
                station_id=str(plan["source_station_id"]),
                target_local_date=day,
                run_initialization_utc=run,
                valid_time_utc=valid,
                decision_at_utc=None,
                target_timezone=str(plan["target_timezone"]),
                output_root=raw_root.expanduser().resolve(),
                timeout_sec=timeout_sec,
                max_response_bytes=int(budget["max_response_bytes"]),
                max_capture_bytes=int(budget["max_capture_bytes"]),
                min_free_bytes=int(budget["min_free_bytes"]),
                range_start=start,
                range_end=end,
                run_contract=plan["model_run_contract"],
            )
            record = {
                "forecast_hour": point["forecast_hour"],
                "valid_time_utc": valid,
                "status": captured.get("status"),
                "transport_receipt_path": captured.get("transport_receipt_path"),
                "index_sha256": hashlib.sha256(index_body).hexdigest(),
                "index_last_modified": index_headers.get("last-modified"),
                "selected_index_row": index_row,
                "request_byte_range": {"start": start, "end": end},
                "reason_codes": (captured.get("receipt") or {}).get("reason_codes", []),
            }
        except (ContractError, OSError, TimeoutError, ValueError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, ContractError)
                else f"{type(exc).__name__}:{exc}"
            )
            record = {
                "forecast_hour": point["forecast_hour"],
                "valid_time_utc": valid,
                "status": "BLOCKED",
                "transport_receipt_path": None,
                "reason_codes": [reason],
            }
        records.append(record)
        write_bounded_json(
            destination / f"capture_{valid.replace(':', '').replace('-', '')}.json",
            record,
        )
    usable = [item for item in records if item["status"] in {"CAPTURED", "REUSED"}]
    fanout_result = None
    if usable:
        fanout_result = fanout(
            provider=PROVIDER,
            target_local_date=day,
            run_initialization_utc=run,
            decision_at_utc=_stamp(datetime.now(timezone.utc)),
            target_timezone=str(plan["target_timezone"]),
            station_ids=[str(value) for value in plan["station_ids"]],
            source_station_id=str(plan["source_station_id"]),
            source_root=raw_root,
            output_dir=destination / "station_arrivals",
        )
    result_identity = {
        "schema": SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "target_local_date": day,
        "target_timezone": str(plan["target_timezone"]),
        "run_initialization_utc": run,
        "expected_capture_count": len(plan["points"]),
        "captured_or_reused_count": len(usable),
        "blocked_count": sum(item["status"] not in {"CAPTURED", "REUSED"} for item in records),
        "records": records,
        "fanout": fanout_result,
        "resource_budget": budget,
        "boundary": dict(plan["boundary"]),
    }
    result = {**result_identity, "receipt_sha256": content_sha256(result_identity)}
    write_bounded_json(destination / "manifest.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--capture-config", type=Path, default=DEFAULT_CAPTURE_CONFIG)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    plan = build_plan(
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        as_of_utc=args.as_of_utc,
        contract_path=args.contract,
        capture_config_path=args.capture_config,
    )
    value = execute_plan(
        plan,
        raw_root=args.raw_root,
        output_root=args.output_root,
        timeout_sec=args.timeout_sec,
    ) if args.execute else plan
    if args.output:
        write_bounded_json(args.output.expanduser().resolve(), value)
    print(json.dumps({
        "schema": value["schema"],
        "target_local_date": value["target_local_date"],
        "run_initialization_utc": value["run_initialization_utc"],
        "point_count": len(value.get("points") or value.get("records") or []),
        "executed": bool(args.execute),
        "calls_order_gateway": value["boundary"]["calls_order_gateway"],
    }, sort_keys=True))
    return 0 if not args.execute or int(value.get("blocked_count") or 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
