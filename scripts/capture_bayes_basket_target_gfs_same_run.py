#!/usr/bin/env python3
"""Capture bounded official GFS curves for the contract-scoped stations.

The target curve samples ET local hours 02, 08, 14, 20 and 23 from one
official run.  Forecast hours are derived from the target ET date and run
initialization instead of being tied to a same-UTC-date run.  The five points
cover the fixed ET day with no more than six-hour gaps.  NOAA NOMADS responses
remain station-filtered; this command never fans one station response out to
another station.

The command is target-only and prospective. It writes only content-addressed
target lineage raw/receipt objects and small audit packets eligible for formal
``production_no_live`` probability input; it never writes SQLite, shared
production storage, service state, or an order gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import (  # noqa: E402
    validate_model_arrival_capture,
    validate_prospective_capture_window,
)
from bayes_basket_target.model_arrival_capture_config import load_target_capture_config  # noqa: E402
from bayes_basket_target.model_curve_schedule import aligned_target_sample_offsets  # noqa: E402
from bayes_basket_target.model_run_contract import validate_capture_request  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest, station_coordinates  # noqa: E402
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402
from official_nwp_https_transport import open_official_https, read_official_response  # noqa: E402


PROVIDER = "gfs_0p25_official"
MODEL_ID = "GFS"
DEFAULT_RAW_ROOT = ROOT / "data" / "research" / "bayes_basket_target" / "model_arrivals"
DEFAULT_CAPTURE_CONFIG = ROOT / "config" / "bayes_basket_target" / "model_arrival_capture_v1.json"
MAX_GFS_FORECAST_HOUR = 384
SCHEMA = "bayes_basket_target_gfs_same_run_capture_v1"
MAX_CONSECUTIVE_IDENTICAL_BLOCKERS = 5
MAX_GFS_INDEX_BYTES = 1024 * 1024
GFS_INDEX_TIMEOUT_SEC = 20.0


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def forecast_hours_for_target_settlement_day(
    *,
    target_local_date: str,
    run_initialization: datetime,
    target_timezone: str,
) -> tuple[int, ...]:
    """Map the registry settlement day onto the shared ECMWF/GFS grid."""

    window = settlement_day_window(target_local_date, target_timezone)
    start = _parse_utc(window["target_day_start_utc"])
    if run_initialization.astimezone(timezone.utc) > start:
        raise ValueError("model_curve_run_newer_than_target_sample")
    offsets = aligned_target_sample_offsets(
        target_day_start_utc=start,
        run_initialization=run_initialization,
    )
    forecast_hours = tuple(
        int(((start + timedelta(hours=offset)) - run_initialization.astimezone(timezone.utc)).total_seconds() // 3600)
        for offset in offsets
    )
    if min(forecast_hours) < 0:
        raise ValueError("model_curve_run_newer_than_target_sample")
    if max(forecast_hours) > MAX_GFS_FORECAST_HOUR:
        raise ValueError("model_curve_forecast_hour_exceeds_supported_horizon")
    return forecast_hours


def build_gfs_urls(*, run_initialization: datetime, forecast_hour: int, latitude: float, longitude: float) -> tuple[str, str]:
    run_day = run_initialization.strftime("%Y%m%d")
    run_hour = run_initialization.strftime("%H")
    filename = f"gfs.t{run_hour}z.pgrb2.0p25.f{forecast_hour:03d}"
    left = longitude - 0.5
    right = longitude + 0.5
    bottom = latitude - 0.5
    top = latitude + 0.5
    query = urlencode({
        "file": filename,
        "lev_2_m_above_ground": "on",
        "lev_850_mb": "on",
        "lev_700_mb": "on",
        "lev_500_mb": "on",
        "var_TMP": "on",
        "var_RH": "on",
        "var_HGT": "on",
        "var_UGRD": "on",
        "var_VGRD": "on",
        "subregion": "",
        "leftlon": f"{left:.2f}",
        "rightlon": f"{right:.2f}",
        "toplat": f"{top:.2f}",
        "bottomlat": f"{bottom:.2f}",
        "dir": f"/gfs.{run_day}/{run_hour}/atmos",
    })
    filtered = f"https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?{query}"
    publication = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run_day}/{run_hour}/atmos/{filename}"
    return filtered, publication


def build_gfs_native_object_url(*, run_initialization: datetime, forecast_hour: int) -> str:
    run_day = run_initialization.strftime("%Y%m%d")
    run_hour = run_initialization.strftime("%H")
    filename = f"gfs.t{run_hour}z.pgrb2.0p25.f{forecast_hour:03d}"
    return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run_day}/{run_hour}/atmos/{filename}"


def parse_gfs_2m_temperature_range(index_payload: bytes) -> tuple[int, int]:
    """Resolve exactly one native 2-m temperature GRIB message from NOAA idx."""

    if len(index_payload) > MAX_GFS_INDEX_BYTES:
        raise ValueError("gfs_index_response_too_large")
    try:
        lines = index_payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("gfs_index_utf8_required") from exc
    matches: list[tuple[int, int]] = []
    for position, line in enumerate(lines):
        if ":TMP:2 m above ground:" not in line:
            continue
        fields = line.split(":")
        if len(fields) < 5:
            raise ValueError("gfs_index_record_invalid")
        try:
            start = int(fields[1])
            next_start = int(lines[position + 1].split(":")[1])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("gfs_index_next_record_required") from exc
        if start < 0 or next_start <= start:
            raise ValueError("gfs_index_byte_range_invalid")
        matches.append((start, next_start - 1))
    if len(matches) != 1:
        raise ValueError("gfs_index_2m_temperature_record_not_unique")
    return matches[0]


def resolve_gfs_2m_temperature_range(
    *,
    run_initialization: datetime,
    forecast_hour: int,
    timeout_sec: float = GFS_INDEX_TIMEOUT_SEC,
) -> tuple[str, int, int]:
    """Read a bounded official NOAA index and return one native byte range."""

    object_url = build_gfs_native_object_url(
        run_initialization=run_initialization,
        forecast_hour=forecast_hour,
    )
    request = Request(
        object_url + ".idx",
        headers={"User-Agent": "PolymarketBayesBasketTarget/1.0"},
    )
    with open_official_https(request, timeout_sec=timeout_sec) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status != 200:
            raise ValueError("gfs_index_provider_response_invalid")
        advertised = response.headers.get("Content-Length")
        if advertised is not None and int(advertised) > MAX_GFS_INDEX_BYTES:
            raise ValueError("gfs_index_response_too_large")
        payload = read_official_response(
            response,
            timeout_sec=timeout_sec,
            chunk_size=64 * 1024,
        )
    start, end = parse_gfs_2m_temperature_range(payload)
    return object_url, start, end


def _existing_matches(
    path: Path,
    *,
    station_id: str,
    target_date: str,
    target_timezone: str,
    run: str,
    valid: str,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> bool:
    try:
        value = load_bounded_json(path)
    except Exception:
        return False
    arrival = value.get("arrival") if isinstance(value, Mapping) else None
    boundary = value.get("boundary") if isinstance(value, Mapping) else None
    request_range = (
        boundary.get("request_byte_range")
        if isinstance(boundary, Mapping)
        else None
    )
    transport_path = (
        Path(str(value.get("transport_receipt_path") or ""))
        .expanduser()
        .resolve()
        if isinstance(value, Mapping)
        and str(value.get("transport_receipt_path") or "").strip()
        else None
    )
    raw_intact = False
    if transport_path is not None and transport_path.is_file():
        try:
            transport = load_bounded_json(transport_path)
            raw_receipts = transport.get("raw_receipts")
            if isinstance(raw_receipts, list) and raw_receipts:
                raw_intact = True
                resolved_root = raw_root.expanduser().resolve()
                for receipt in raw_receipts:
                    if not isinstance(receipt, Mapping):
                        raw_intact = False
                        break
                    raw_path = Path(
                        str(receipt.get("raw_object_ref") or "")
                    ).expanduser().resolve()
                    raw_sha = str(receipt.get("raw_sha256") or "")
                    try:
                        raw_path.relative_to(resolved_root)
                    except ValueError:
                        raw_intact = False
                        break
                    if (
                        len(raw_sha) != 64
                        or not raw_path.is_file()
                        or _sha256_file(raw_path) != raw_sha
                    ):
                        raw_intact = False
                        break
        except Exception:
            raw_intact = False
    return bool(
        isinstance(value, Mapping)
        and value.get("status") == "CAPTURED"
        and isinstance(arrival, Mapping)
        and isinstance(boundary, Mapping)
        and isinstance(request_range, Mapping)
        and isinstance(request_range.get("start"), int)
        and isinstance(request_range.get("end"), int)
        and request_range["end"] >= request_range["start"]
        and arrival.get("provider_id") == PROVIDER
        and str(arrival.get("station_id") or "").upper() == station_id
        and arrival.get("target_local_date") == target_date
        and arrival.get("target_timezone") == target_timezone
        and arrival.get("run_initialization_utc") == run
        and arrival.get("valid_time_utc") == valid
        and str(arrival.get("source_url") or "").startswith(
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        )
        and boundary.get("writes_production_db") is False
        and boundary.get("calls_order_gateway") is False
        and boundary.get("places_orders") is False
        and raw_intact
    )


def _derive_station_capture_from_global_field(
    template: Mapping[str, Any],
    *,
    station_id: str,
    target_local_date: str,
    run_initialization_utc: str,
    valid_time_utc: str,
    raw_root: Path,
) -> dict[str, Any]:
    """Bind one observed native global field to a distinct station lineage."""

    template_receipt_path = Path(str(template.get("transport_receipt_path") or "")).expanduser().resolve()
    receipt = load_bounded_json(template_receipt_path)
    if not isinstance(receipt, Mapping):
        raise ValueError("gfs_global_template_receipt_mapping_required")
    identity = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_content_sha256"
    }
    if (
        identity.get("provider_id") != PROVIDER
        or identity.get("target_local_date") != target_local_date
        or identity.get("run_initialization_utc") != run_initialization_utc
        or identity.get("valid_time_utc") != valid_time_utc
    ):
        raise ValueError("gfs_global_template_lineage_mismatch")
    identity["station_id"] = station_id
    identity["capture_scope"] = "global_native_2m_temperature_field"
    identity["station_derivation"] = "same_observed_global_field_distinct_station_decode"
    identity["source_transport_receipt_path"] = str(template_receipt_path)
    receipt_digest = content_sha256(identity)
    receipt_path = raw_root / PROVIDER / "receipts" / f"{receipt_digest}.json"
    write_bounded_json(
        receipt_path,
        {**identity, "receipt_content_sha256": receipt_digest},
    )
    receipt_file_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    template_arrival = template.get("arrival")
    if not isinstance(template_arrival, Mapping):
        raise ValueError("gfs_global_template_arrival_mapping_required")
    arrival_decision = str(template_arrival.get("decision_at_utc") or "")
    arrival = validate_model_arrival_capture(
        {
            **template_arrival,
            "arrival_id": "arrival-" + receipt_digest[:24],
            "station_id": station_id,
            "receipt_sha256": receipt_file_sha256,
        },
        decision_at_utc=arrival_decision,
    )
    return {
        "status": "CAPTURED",
        "arrival": arrival,
        "transport_receipt_path": str(receipt_path.resolve()),
        "boundary": {
            **dict(template.get("boundary") or {}),
            "global_native_field_reused": True,
            "network_request_reused": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        },
    }


def capture_run(
    *,
    target_local_date: str,
    run_initialization_utc: str,
    decision_at_utc: str,
    contract_path: Path,
    capture_config_path: Path = DEFAULT_CAPTURE_CONFIG,
    raw_root: Path = DEFAULT_RAW_ROOT,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    run = _parse_utc(run_initialization_utc)
    decision = _parse_utc(decision_at_utc)
    contract = load_bounded_json(contract_path.expanduser().resolve())
    if not isinstance(contract, Mapping):
        raise ValueError("model_run_contract_mapping_required")
    target_timezone = str(contract.get("target_timezone") or "").strip()
    stations = [
        str(station_id).strip().upper()
        for station_id in contract.get("station_ids") or []
        if str(station_id).strip()
    ]
    if not target_timezone:
        raise ValueError("model_run_contract_timezone_missing")
    if not stations:
        raise ValueError("model_run_contract_station_scope_missing")
    if not str(contract.get("market_registry_snapshot_sha256") or ""):
        raise ValueError("model_run_contract_registry_lineage_missing")
    capture_window = validate_prospective_capture_window(
        target_local_date=target_local_date,
        target_timezone=target_timezone,
        run_initialization_utc=_stamp(run),
        decision_at_utc=decision_at_utc,
    )
    forecast_hours = forecast_hours_for_target_settlement_day(
        target_local_date=target_local_date,
        run_initialization=run,
        target_timezone=target_timezone,
    )
    capture_config = load_target_capture_config(capture_config_path)
    resource_budget = dict(capture_config["resource_budget"])
    assert_process_memory_budget(limit=resource_budget["max_process_rss_bytes"])
    assert_target_raw_retention_budget(
        raw_root,
        bytes_needed=0,
        limit=resource_budget["max_raw_bytes"],
        min_free_bytes=resource_budget["min_free_bytes"],
    )
    for station_id in stations:
        validate_capture_request(
            contract,
            provider_id=PROVIDER,
            station_id=station_id,
            target_local_date=target_local_date,
            run_initialization_utc=_stamp(run),
            target_timezone=target_timezone,
        )
    capture_module = importlib.import_module("scripts.capture_bayes_basket_target_model_arrival")
    # Resolve native indexes only for genuinely missing points.  A complete
    # content-addressed capture must replay without network access; otherwise
    # transient proxy failures turn deterministic reuse into needless
    # downloads and can loop until the resource guard fires.
    source_ranges: dict[int, tuple[str, int, int]] = {}
    destination = (output_dir or ROOT / "output" / "audits" / f"bayes_basket_target_gfs_same_run_{run.strftime('%Y%m%d_%Hz')}").expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    global_field_cache: dict[int, dict[str, Any]] = {}
    consecutive_blocker_signature: tuple[str, ...] | None = None
    consecutive_identical_blockers = 0
    early_stop: dict[str, Any] | None = None
    for station_id in stations:
        station_coordinates(station_id)
        for forecast_hour in forecast_hours:
            assert_process_memory_budget(limit=resource_budget["max_process_rss_bytes"])
            assert_target_raw_retention_budget(
                raw_root,
                bytes_needed=resource_budget["max_response_bytes"],
                limit=resource_budget["max_raw_bytes"],
                min_free_bytes=resource_budget["min_free_bytes"],
            )
            valid_time = run + timedelta(hours=forecast_hour)
            valid_text = _stamp(valid_time)
            output_path = destination / f"{station_id}_{valid_text.replace(':', '').replace('-', '')}.json"
            if output_path.is_file() and _existing_matches(
                output_path,
                station_id=station_id,
                target_date=target_local_date,
                target_timezone=target_timezone,
                run=_stamp(run),
                valid=valid_text,
                raw_root=raw_root,
            ):
                records.append({"path": str(output_path), "status": "REUSED"})
                existing_capture = load_bounded_json(output_path)
                existing_arrival = existing_capture.get("arrival")
                existing_boundary = existing_capture.get("boundary")
                existing_range = existing_boundary.get("request_byte_range")
                source_ranges[forecast_hour] = (
                    str(existing_arrival["source_url"]),
                    int(existing_range["start"]),
                    int(existing_range["end"]),
                )
                if forecast_hour not in global_field_cache:
                    if (
                        isinstance(existing_capture, Mapping)
                        and existing_capture.get("status") == "CAPTURED"
                        and existing_capture.get("transport_receipt_path")
                    ):
                        global_field_cache[forecast_hour] = dict(existing_capture)
                consecutive_blocker_signature = None
                consecutive_identical_blockers = 0
                continue
            source_ranges[forecast_hour] = resolve_gfs_2m_temperature_range(
                run_initialization=run,
                forecast_hour=forecast_hour,
            )
            publication_url, range_start, range_end = source_ranges[forecast_hour]
            cached = global_field_cache.get(forecast_hour)
            if cached is not None:
                result = _derive_station_capture_from_global_field(
                    cached,
                    station_id=station_id,
                    target_local_date=target_local_date,
                    run_initialization_utc=_stamp(run),
                    valid_time_utc=valid_text,
                    raw_root=raw_root.expanduser().resolve(),
                )
            else:
                result = capture_module.capture(
                    provider_id=PROVIDER,
                    urls=[publication_url],
                    publication_url=publication_url,
                    station_id=station_id,
                    target_local_date=target_local_date,
                    run_initialization_utc=_stamp(run),
                    valid_time_utc=valid_text,
                    # Bind the formal decision clock to the observed response
                    # completion. Passing the pre-request planning clock would
                    # reject every successful download as received-after-decision.
                    decision_at_utc=None,
                    target_timezone=target_timezone,
                    output_root=raw_root.expanduser().resolve(),
                    timeout_sec=45.0,
                    max_response_bytes=resource_budget["max_response_bytes"],
                    max_capture_bytes=resource_budget["max_capture_bytes"],
                    min_free_bytes=resource_budget["min_free_bytes"],
                    range_start=range_start,
                    range_end=range_end,
                    run_contract=contract,
                )
                if (
                    result.get("status") == "CAPTURED"
                    and result.get("transport_receipt_path")
                ):
                    global_field_cache[forecast_hour] = result
            status = str(result.get("status") or "BLOCKED")
            if status == "CAPTURED":
                write_bounded_json(output_path, result)
                record = {"path": str(output_path), "status": status}
                consecutive_blocker_signature = None
                consecutive_identical_blockers = 0
            else:
                reason_codes = tuple(
                    sorted(
                        {
                            str(value)
                            for value in result.get("receipt", {}).get("reason_codes", [])
                            if str(value)
                        }
                    )
                ) or ("model_arrival_blocked",)
                record = {
                    "station_id": station_id,
                    "valid_time_utc": valid_text,
                    "status": status,
                    "reason_codes": list(reason_codes),
                    "failed_fragment_retained": False,
                }
                blockers.append(record)
                if reason_codes == consecutive_blocker_signature:
                    consecutive_identical_blockers += 1
                else:
                    consecutive_blocker_signature = reason_codes
                    consecutive_identical_blockers = 1
            records.append(record)
            if consecutive_identical_blockers >= MAX_CONSECUTIVE_IDENTICAL_BLOCKERS:
                early_stop = {
                    "reason": "consecutive_identical_transport_blockers",
                    "threshold": MAX_CONSECUTIVE_IDENTICAL_BLOCKERS,
                    "consecutive_count": consecutive_identical_blockers,
                    "reason_codes": list(consecutive_blocker_signature or ()),
                    "attempted_count": len(records),
                }
                break
            if len(records) % 25 == 0:
                print(json.dumps({"completed": len(records), "total": len(stations) * len(forecast_hours), "blockers": len(blockers)}, sort_keys=True), flush=True)
        if early_stop is not None:
            break
    manifest = {
        "schema": SCHEMA,
        "target_local_date": target_local_date,
        "target_timezone": target_timezone,
        "run_initialization_utc": _stamp(run),
        "decision_at_utc": _stamp(decision),
        "station_count": len(stations),
        "forecast_hours": list(forecast_hours),
        "forecast_hour_count": len(forecast_hours),
        "source_transport": "official_noaa_s3_idx_http_range",
        "network_capture_count": len(global_field_cache),
        "station_lineage_from_global_native_field": True,
        "source_ranges": {
            str(forecast_hour): {
                "url": source_ranges[forecast_hour][0],
                "range_start": source_ranges[forecast_hour][1],
                "range_end": source_ranges[forecast_hour][2],
                "response_bytes": source_ranges[forecast_hour][2] - source_ranges[forecast_hour][1] + 1,
            }
            for forecast_hour in forecast_hours
        },
        "expected_capture_count": len(stations) * len(forecast_hours),
        "captured_or_reused_count": sum(item["status"] in {"CAPTURED", "REUSED"} for item in records),
        "blocked_count": len(blockers),
        "attempted_count": len(records),
        "early_stopped": early_stop is not None,
        "early_stop": early_stop,
        "resource_budget": resource_budget,
        "capture_config_path": str(capture_config_path.expanduser().resolve()),
        "output_dir": str(destination),
        "records": records,
        "blockers": blockers,
        "boundary": {
            "target_only": True,
            "execution_mode": "production_no_live",
            "formal_probability_input_role_eligible": True,
            "p0_probability_input_eligible_now": True,
            "p1_probability_input_eligible_now": capture_window["state"] == "open",
            "raw_capture_may_precede_event": True,
            "p0_consumption_before_event": True,
            "p1_consumption_before_event": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "authorizes_order": False,
            "live_order_authorized": False,
            "live_submit_authorized": False,
            "unrelated_production_line_touched": False,
        },
    }
    manifest_path = destination / "manifest.json"
    write_bounded_json(manifest_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        required=True,
        help="Exact registry-scoped model-run contract for this target date/run",
    )
    parser.add_argument("--capture-config", type=Path, default=DEFAULT_CAPTURE_CONFIG)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    manifest = capture_run(
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        decision_at_utc=args.decision_at_utc,
        contract_path=args.contract,
        capture_config_path=args.capture_config,
        raw_root=args.raw_root,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "schema": manifest["schema"],
        "expected_capture_count": manifest["expected_capture_count"],
        "captured_or_reused_count": manifest["captured_or_reused_count"],
        "blocked_count": manifest["blocked_count"],
        "calls_order_gateway": manifest["boundary"]["calls_order_gateway"],
        "writes_production_db": manifest["boundary"]["writes_production_db"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not manifest["blockers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
