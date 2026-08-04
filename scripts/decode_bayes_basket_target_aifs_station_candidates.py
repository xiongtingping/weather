#!/usr/bin/env python3
"""Materialize 45-station AIFS Single candidate curves from retained raw.

This is deliberately a decode-only continuation of the official AIFS forward
capture.  It accepts only already retained ECMWF Open Data ``aifs-single``
2-metre-temperature GRIB ranges and their registry-bound station-arrival
receipts.  It never downloads upstream data, writes SQLite, activates P0, or
touches an order gateway.

The output is a prospective official-native *candidate* artifact.  AIFS
Single remains a deterministic ECMWF-family product: it is not an ensemble,
does not add an independent family beside IFS, and cannot become formal P0
without its separate strict-OOS/provider binding.  The available Open-Meteo
AIFS Global and Europe ensembles are reported only as explicit
``aggregator_republisher`` variants; this adapter does not relabel or upgrade
them.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import validate_model_arrival_capture  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    decode_native_temperature_points,
    materialize_native_daily_curves_by_station_timezone,
)
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIB,
    ResourceBoundError,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_aifs_station_candidate_evidence_v1"
PROVIDER_ID = "ecmwf_aifs025_official"
PRODUCT_ID = "ECMWF_AIFS_SINGLE_0P25"
INDEPENDENCE_FAMILY_ID = "ECMWF"
OFFICIAL_AIFS_HOST = "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"

DEFAULT_CAPTURE = ROOT / "output/audits/aifs_official_forward_capture_20260801_full45.json"
DEFAULT_OUTPUT = ROOT / "output/audits/aifs_official_station_candidates_20260801.json"

MAX_STATIONS = 45
MAX_CAPTURE_OUTPUTS = 512
MAX_AIFS_RAW_OBJECTS = 8
MAX_AIFS_POINTS = MAX_STATIONS * 8
MAX_WORKERS = 4
MAX_PROCESS_RSS_BYTES = 512 * MIB
MIN_FREE_BYTES = 512 * MIB
RAW_OBJECT_SOFT_LIMIT_BYTES = 64 * MIB
RAW_OBJECT_HARD_LIMIT_BYTES = 128 * MIB
RAW_TOTAL_HARD_LIMIT_BYTES = 256 * MIB
MAX_EVIDENCE_OUTPUT_BYTES = 4 * MIB
_SHA256 = re.compile(r"[0-9a-f]{64}")


class AifsCandidateDecodeError(ContractError):
    """Raised when official AIFS candidate lineage is incomplete or unsafe."""


Decoder = Callable[..., Mapping[str, Any]]


def _timestamp(name: str, value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise AifsCandidateDecodeError(f"aifs_candidate_{name}_missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AifsCandidateDecodeError(f"aifs_candidate_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AifsCandidateDecodeError(f"aifs_candidate_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(MIB), b""):
                digest.update(block)
    except OSError as exc:
        raise AifsCandidateDecodeError("aifs_candidate_input_read_failed") from exc
    return digest.hexdigest()


def _resolved_regular_file(path_value: Any, *, code: str) -> Path:
    raw = Path(str(path_value or "")).expanduser()
    if not str(raw):
        raise AifsCandidateDecodeError(code)
    try:
        if raw.is_symlink():
            raise AifsCandidateDecodeError(code)
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise AifsCandidateDecodeError(code) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise AifsCandidateDecodeError(code)
    return resolved


def _require_sha(name: str, value: Any) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256.fullmatch(digest) is None:
        raise AifsCandidateDecodeError(f"aifs_candidate_{name}_invalid")
    return digest


def _validate_official_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != OFFICIAL_AIFS_HOST
        or "/aifs-single/0p25/oper/" not in parsed.path
        or not parsed.path.endswith(".grib2")
    ):
        raise AifsCandidateDecodeError("aifs_candidate_source_not_official_aifs")
    return url


def _target_bounds(event_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        local_day = date.fromisoformat(str(event_date))
        zone = ZoneInfo(str(timezone_name))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AifsCandidateDecodeError("aifs_candidate_station_timezone_invalid") from exc
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )
    return start, start + timedelta(days=1)


def _canonical_station_timezones(capture: Mapping[str, Any]) -> dict[str, str]:
    manifest = coordinate_registry_manifest()
    stations = [str(value) for value in manifest["station_ids"]]
    if len(stations) != MAX_STATIONS or len(set(stations)) != MAX_STATIONS:
        raise AifsCandidateDecodeError("aifs_candidate_coordinate_registry_scope_invalid")
    supplied = capture.get("station_timezones")
    if not isinstance(supplied, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_station_timezones_missing")
    normalized = {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in supplied.items()
        if str(station).strip() and str(timezone_name).strip()
    }
    if set(normalized) != set(stations):
        raise AifsCandidateDecodeError("aifs_candidate_station_timezone_scope_invalid")
    try:
        for timezone_name in normalized.values():
            ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AifsCandidateDecodeError("aifs_candidate_station_timezone_invalid") from exc
    return dict(sorted(normalized.items()))


def _load_capture(capture_path: Path) -> tuple[dict[str, Any], str]:
    path = _resolved_regular_file(capture_path, code="aifs_candidate_capture_missing")
    value = load_bounded_json(path)
    if not isinstance(value, dict):
        raise AifsCandidateDecodeError("aifs_candidate_capture_mapping_required")
    supplied_sha = _require_sha("capture_content_sha256", value.get("content_sha256"))
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if supplied_sha != content_sha256(identity):
        raise AifsCandidateDecodeError("aifs_candidate_capture_content_sha_mismatch")
    if (
        value.get("schema") != "bayes_basket_target_aifs_forward_capture_scheduler_v1"
        or value.get("state") != "CAPTURED_NATIVE_FORWARD_CANDIDATE"
        or value.get("provider_id") != PROVIDER_ID
        or value.get("product_id") != PRODUCT_ID
        or value.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or int(value.get("blocked_count") or 0) != 0
    ):
        raise AifsCandidateDecodeError("aifs_candidate_capture_contract_invalid")
    authority = value.get("authority")
    boundary = value.get("boundary")
    if (
        not isinstance(authority, Mapping)
        or authority.get("source_role") != "official_native"
        or authority.get("native_arrival_index_eligible") is not True
        or authority.get("formal_p0_eligible_without_strict_oos") is not False
        or authority.get("counts_as_independent_family_from_ifs") is not False
        or not isinstance(boundary, Mapping)
        or boundary.get("writes_target_database") is not False
        or boundary.get("writes_production_db") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
        or boundary.get("live_submit_authorized") is not False
        or boundary.get("promotion_allowed") is not False
    ):
        raise AifsCandidateDecodeError("aifs_candidate_capture_authority_invalid")
    _timestamp("capture_execution_decision_at_utc", value.get("execution_decision_at_utc"))
    _timestamp("capture_run_initialization_utc", value.get("run_initialization_utc"))
    if not str(value.get("target_local_date") or "").strip():
        raise AifsCandidateDecodeError("aifs_candidate_capture_target_date_missing")
    _canonical_station_timezones(value)
    fanout = value.get("fanout")
    if not isinstance(fanout, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_fanout_missing")
    outputs = fanout.get("outputs")
    if (
        not isinstance(outputs, list)
        or not outputs
        or len(outputs) > MAX_CAPTURE_OUTPUTS
        or int(fanout.get("blocked_count") or 0) != 0
        or int(fanout.get("captured_count") or 0) != len(outputs)
        or int(fanout.get("expected_arrival_count") or 0) != len(outputs)
        or int(fanout.get("station_count") or 0) != MAX_STATIONS
    ):
        raise AifsCandidateDecodeError("aifs_candidate_fanout_incomplete")
    return value, _sha256_file(path)


def _transport_receipt(
    receipt_path_value: Any,
    *,
    expected_raw_sha256: str,
    expected_run_initialization_utc: str,
    expected_target_local_date: str,
) -> dict[str, Any]:
    receipt_path = _resolved_regular_file(
        receipt_path_value, code="aifs_candidate_transport_receipt_missing"
    )
    receipt = load_bounded_json(receipt_path)
    if not isinstance(receipt, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_transport_receipt_mapping_required")
    supplied = _require_sha("transport_receipt_content_sha256", receipt.get("receipt_content_sha256"))
    identity = {
        key: value for key, value in receipt.items() if key != "receipt_content_sha256"
    }
    if supplied != content_sha256(identity):
        raise AifsCandidateDecodeError("aifs_candidate_transport_receipt_sha_mismatch")
    if (
        receipt.get("provider_id") != PROVIDER_ID
        or receipt.get("target_local_date") != expected_target_local_date
        or receipt.get("run_initialization_utc") != expected_run_initialization_utc
        or receipt.get("capture_mode") != "prospective"
    ):
        raise AifsCandidateDecodeError("aifs_candidate_transport_receipt_lineage_invalid")
    raw_receipts = receipt.get("raw_receipts")
    if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
        raise AifsCandidateDecodeError("aifs_candidate_transport_raw_receipt_invalid")
    raw = raw_receipts[0]
    if not isinstance(raw, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_transport_raw_receipt_invalid")
    raw_sha = _require_sha("raw_sha256", raw.get("raw_sha256"))
    if raw_sha != expected_raw_sha256:
        raise AifsCandidateDecodeError("aifs_candidate_transport_raw_sha_mismatch")
    raw_path = _resolved_regular_file(
        raw.get("raw_object_ref"), code="aifs_candidate_raw_object_missing"
    )
    if raw_path.suffix.lower() != ".grib2":
        raise AifsCandidateDecodeError("aifs_candidate_raw_object_suffix_invalid")
    try:
        raw_bytes = int(raw_path.stat().st_size)
    except OSError as exc:
        raise AifsCandidateDecodeError("aifs_candidate_raw_object_stat_failed") from exc
    if raw_bytes <= 0 or raw_bytes > RAW_OBJECT_HARD_LIMIT_BYTES:
        raise AifsCandidateDecodeError("aifs_candidate_raw_object_hard_limit_exceeded")
    if _sha256_file(raw_path) != raw_sha:
        raise AifsCandidateDecodeError("aifs_candidate_raw_object_sha_mismatch")
    try:
        response_bytes = int(raw.get("response_bytes") or -1)
    except (TypeError, ValueError) as exc:
        raise AifsCandidateDecodeError("aifs_candidate_raw_response_bytes_invalid") from exc
    if response_bytes != raw_bytes:
        raise AifsCandidateDecodeError("aifs_candidate_raw_response_bytes_mismatch")
    headers = raw.get("response_headers")
    if not isinstance(headers, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_raw_response_headers_missing")
    advertised = headers.get("content-length", headers.get("Content-Length"))
    try:
        if advertised not in (None, "") and int(advertised) != raw_bytes:
            raise AifsCandidateDecodeError("aifs_candidate_raw_content_length_mismatch")
    except (TypeError, ValueError) as exc:
        raise AifsCandidateDecodeError("aifs_candidate_raw_content_length_invalid") from exc
    source_url = _validate_official_url(raw.get("raw_request_url"))
    published_at = _timestamp("raw_published_at_utc", raw.get("provider_published_at_utc"))
    received_at = _timestamp("raw_received_at_utc", raw.get("response_completed_at"))
    if published_at > received_at:
        raise AifsCandidateDecodeError("aifs_candidate_raw_arrival_chronology_invalid")
    free_bytes = int(shutil.disk_usage(raw_path.parent).free)
    if free_bytes < MIN_FREE_BYTES:
        raise AifsCandidateDecodeError("aifs_candidate_raw_min_free_space_guard")
    return {
        "path": receipt_path,
        "sha256": supplied,
        "raw_path": raw_path,
        "raw_sha256": raw_sha,
        "raw_bytes": raw_bytes,
        "raw_soft_limit_exceeded": raw_bytes > RAW_OBJECT_SOFT_LIMIT_BYTES,
        "source_url": source_url,
        "published_at_utc": _timestamp_text(published_at),
        "received_at_utc": _timestamp_text(received_at),
    }


def load_station_arrivals(capture_path: Path) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Load and validate bounded per-station AIFS arrival evidence."""

    capture, capture_sha = _load_capture(capture_path)
    station_timezones = _canonical_station_timezones(capture)
    event_date = str(capture["target_local_date"])
    run = _timestamp_text(_timestamp("capture_run_initialization_utc", capture["run_initialization_utc"]))
    decision_at = _timestamp("capture_execution_decision_at_utc", capture["execution_decision_at_utc"])
    arrivals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    fanout = capture["fanout"]
    assert isinstance(fanout, Mapping)
    outputs = fanout["outputs"]
    assert isinstance(outputs, list)
    for output in outputs:
        if not isinstance(output, Mapping) or output.get("status") != "CAPTURED":
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_status_invalid")
        arrival_path = _resolved_regular_file(
            output.get("path"), code="aifs_candidate_station_arrival_missing"
        )
        payload = load_bounded_json(arrival_path)
        if not isinstance(payload, Mapping) or payload.get("status") != "CAPTURED":
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_payload_invalid")
        raw_arrival = payload.get("arrival")
        if not isinstance(raw_arrival, Mapping):
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_payload_invalid")
        try:
            arrival = validate_model_arrival_capture(raw_arrival)
        except ContractError as exc:
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_invalid") from exc
        station_id = str(arrival.get("station_id") or "").strip().upper()
        valid_time = _timestamp_text(_timestamp("arrival_valid_time_utc", arrival.get("valid_time_utc")))
        if (
            station_id not in station_timezones
            or arrival.get("provider_id") != PROVIDER_ID
            or arrival.get("model_id") != "ECMWF"
            or arrival.get("target_local_date") != event_date
            or arrival.get("target_timezone") != station_timezones[station_id]
            or _timestamp_text(_timestamp("arrival_run_initialization_utc", arrival.get("run_initialization_utc"))) != run
            or _timestamp("arrival_received_at_utc", arrival.get("received_at_utc")) > decision_at
            or _timestamp("arrival_published_at_utc", arrival.get("published_at_utc")) > decision_at
            or arrival.get("capture_mode") != "prospective"
            or arrival.get("aggregator_or_republisher") is not False
            or arrival.get("open_meteo") is not False
        ):
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_lineage_invalid")
        _validate_official_url(arrival.get("source_url"))
        raw_payload_sha256s = arrival.get("raw_payload_sha256s")
        if not isinstance(raw_payload_sha256s, list) or len(raw_payload_sha256s) != 1:
            raise AifsCandidateDecodeError("aifs_candidate_station_arrival_raw_invalid")
        raw_sha = _require_sha("arrival_raw_sha256", raw_payload_sha256s[0])
        key = (station_id, valid_time)
        if key in seen:
            raise AifsCandidateDecodeError("aifs_candidate_station_valid_duplicate")
        seen.add(key)
        start, end = _target_bounds(event_date, station_timezones[station_id])
        parsed_valid = _timestamp("arrival_valid_time_utc", valid_time)
        if not start <= parsed_valid < end:
            raise AifsCandidateDecodeError("aifs_candidate_station_valid_outside_local_day")
        transport = _transport_receipt(
            payload.get("transport_receipt_path"),
            expected_raw_sha256=raw_sha,
            expected_run_initialization_utc=run,
            expected_target_local_date=event_date,
        )
        if (
            transport["source_url"] != str(arrival.get("source_url"))
            or transport["published_at_utc"] != _timestamp_text(_timestamp("arrival_published_at_utc", arrival.get("published_at_utc")))
            or transport["received_at_utc"] != _timestamp_text(_timestamp("arrival_received_at_utc", arrival.get("received_at_utc")))
        ):
            raise AifsCandidateDecodeError("aifs_candidate_station_transport_binding_invalid")
        arrivals.append({
            "station_id": station_id,
            "event_date": event_date,
            "target_timezone": station_timezones[station_id],
            "valid_time_utc": valid_time,
            "model_run_initialization_utc": run,
            "published_at_utc": transport["published_at_utc"],
            "received_at_utc": transport["received_at_utc"],
            "source_url": transport["source_url"],
            "raw_message_sha256": raw_sha,
            "arrival_receipt_sha256": _require_sha("arrival_receipt_sha256", arrival.get("receipt_sha256")),
            "arrival_payload_sha256": _sha256_file(arrival_path),
            "transport_receipt_sha256": transport["sha256"],
            "transport_receipt_path": str(transport["path"]),
            "raw_path": transport["raw_path"],
            "raw_bytes": int(transport["raw_bytes"]),
            "raw_soft_limit_exceeded": bool(transport["raw_soft_limit_exceeded"]),
        })
    if len(arrivals) != len(outputs):
        raise AifsCandidateDecodeError("aifs_candidate_station_arrival_count_invalid")
    by_station: dict[str, list[dict[str, Any]]] = {station: [] for station in station_timezones}
    for item in arrivals:
        by_station[item["station_id"]].append(item)
    if any(len(rows) < 3 or len(rows) > 8 for rows in by_station.values()):
        raise AifsCandidateDecodeError("aifs_candidate_station_valid_scope_incomplete")
    raw_objects = {str(item["raw_message_sha256"]): item for item in arrivals}
    if not 4 <= len(raw_objects) <= MAX_AIFS_RAW_OBJECTS:
        raise AifsCandidateDecodeError("aifs_candidate_raw_object_scope_invalid")
    total_raw_bytes = sum(int(item["raw_bytes"]) for item in raw_objects.values())
    if total_raw_bytes > RAW_TOTAL_HARD_LIMIT_BYTES:
        raise AifsCandidateDecodeError("aifs_candidate_raw_total_hard_limit_exceeded")
    assert_process_memory_budget(limit=MAX_PROCESS_RSS_BYTES)
    return capture, capture_sha, sorted(arrivals, key=lambda item: (item["valid_time_utc"], item["station_id"]))


def _decode_arrival(
    arrival: Mapping[str, Any],
    *,
    decoder: Decoder,
    grib_get_binary: str | None,
) -> dict[str, Any]:
    assert_process_memory_budget(limit=MAX_PROCESS_RSS_BYTES)
    decoded = decoder(
        Path(str(arrival["raw_path"])),
        provider_id=PROVIDER_ID,
        station_id=str(arrival["station_id"]),
        grib_get_binary=grib_get_binary,
    )
    if not isinstance(decoded, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_mapping_required")
    if (
        decoded.get("provider_id") != PROVIDER_ID
        or decoded.get("model_id") != "ECMWF"
        or str(decoded.get("station_id") or "").upper() != str(arrival["station_id"])
    ):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_identity_invalid")
    decoded_points = decoded.get("points")
    if not isinstance(decoded_points, list) or len(decoded_points) != 1:
        raise AifsCandidateDecodeError("aifs_candidate_decoder_point_count_invalid")
    source_point = decoded_points[0]
    if not isinstance(source_point, Mapping):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_point_invalid")
    run = _timestamp_text(_timestamp("decoder_run_initialization_utc", source_point.get("grib_run_initialization_utc")))
    valid = _timestamp_text(_timestamp("decoder_valid_time_utc", source_point.get("grib_valid_time_utc")))
    if run != str(arrival["model_run_initialization_utc"]) or valid != str(arrival["valid_time_utc"]):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_time_binding_invalid")
    try:
        temperature = float(source_point.get("temperature_2m_c"))
    except (TypeError, ValueError) as exc:
        raise AifsCandidateDecodeError("aifs_candidate_decoder_temperature_invalid") from exc
    if not math.isfinite(temperature):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_temperature_invalid")
    coordinates = decoded.get("coordinate_lineage")
    if not isinstance(coordinates, Mapping) or str(coordinates.get("station_id") or "").upper() != str(arrival["station_id"]):
        raise AifsCandidateDecodeError("aifs_candidate_decoder_coordinate_lineage_invalid")
    assert_process_memory_budget(limit=MAX_PROCESS_RSS_BYTES)
    return {
        "station_id": str(arrival["station_id"]),
        "event_date": str(arrival["event_date"]),
        "target_timezone": str(arrival["target_timezone"]),
        "provider_id": PROVIDER_ID,
        "model_id": "ECMWF",
        "model_run_initialization_utc": run,
        "valid_time_utc": valid,
        "temperature_2m_c": round(temperature, 6),
        "raw_message_sha256": str(arrival["raw_message_sha256"]),
        "receipt_sha256": str(arrival["transport_receipt_sha256"]),
        "arrival_receipt_sha256": str(arrival["arrival_receipt_sha256"]),
        "arrival_payload_sha256": str(arrival["arrival_payload_sha256"]),
        "published_at_utc": str(arrival["published_at_utc"]),
        "received_at_utc": str(arrival["received_at_utc"]),
        "source_url": str(arrival["source_url"]),
        "selector": str(decoded.get("selector") or ""),
        "decoder_id": str(decoded.get("decoder_id") or ""),
        "coordinate_lineage": dict(coordinates),
    }


def _decode_all(
    arrivals: Sequence[Mapping[str, Any]],
    *,
    decoder: Decoder,
    grib_get_binary: str | None,
    workers: int,
) -> list[dict[str, Any]]:
    if not 1 <= int(workers) <= MAX_WORKERS:
        raise AifsCandidateDecodeError("aifs_candidate_worker_count_invalid")
    if len(arrivals) > MAX_AIFS_POINTS:
        raise AifsCandidateDecodeError("aifs_candidate_point_scope_exceeded")
    if workers == 1:
        return [
            _decode_arrival(item, decoder=decoder, grib_get_binary=grib_get_binary)
            for item in arrivals
        ]
    completed: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aifs-decode") as pool:
        futures: dict[Future[dict[str, Any]], Mapping[str, Any]] = {
            pool.submit(
                _decode_arrival,
                item,
                decoder=decoder,
                grib_get_binary=grib_get_binary,
            ): item
            for item in arrivals
        }
        for future in as_completed(futures):
            try:
                completed.append(future.result())
            except Exception as exc:
                for pending in futures:
                    pending.cancel()
                if isinstance(exc, AifsCandidateDecodeError):
                    raise
                raise AifsCandidateDecodeError("aifs_candidate_decoder_failed") from exc
    return sorted(completed, key=lambda item: (item["valid_time_utc"], item["station_id"]))


def _aifs_ensemble_variant_lineage() -> dict[str, dict[str, Any]]:
    variants: dict[str, dict[str, Any]] = {}
    for system_id in ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_ENS_EUROPE"):
        product = ENSEMBLE_SYSTEMS.get(system_id)
        if not isinstance(product, Mapping):
            raise AifsCandidateDecodeError("aifs_candidate_ensemble_catalog_missing")
        if (
            product.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
            or product.get("expected_member_count") != 51
            or product.get("candidate_only") is not True
            or product.get("formal_p0_eligible") is not False
            or product.get("native_arrival_index_eligible") is not False
        ):
            raise AifsCandidateDecodeError("aifs_candidate_ensemble_catalog_authority_invalid")
        variants[system_id] = {
            "api_model": str(product.get("api_model")),
            "canonical_name": str(product.get("canonical_name")),
            "model_scope": str(product.get("model_scope")),
            "expected_member_count": 51,
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "source_role": "aggregator_republisher",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "p1_or_hard_no_eligible": False,
            "official_native_arrival_status": str(product.get("official_native_arrival_status")),
            "materialized_by_this_official_single_decoder": False,
        }
    return variants


def build_candidate_evidence(
    capture_path: Path,
    *,
    grib_get_binary: str | None = None,
    workers: int = MAX_WORKERS,
    decoder: Decoder = decode_native_temperature_points,
) -> dict[str, Any]:
    """Decode every registry-bound official AIFS station arrival once."""

    capture, capture_sha, arrivals = load_station_arrivals(capture_path)
    points = _decode_all(
        arrivals,
        decoder=decoder,
        grib_get_binary=grib_get_binary,
        workers=workers,
    )
    if len(points) != len(arrivals) or len(points) > MAX_AIFS_POINTS:
        raise AifsCandidateDecodeError("aifs_candidate_decoded_point_count_invalid")
    station_timezones = _canonical_station_timezones(capture)
    curves = materialize_native_daily_curves_by_station_timezone(
        points,
        station_timezones=station_timezones,
    )
    if (
        int(curves.get("curve_count") or 0) != MAX_STATIONS
        or int(curves.get("complete_curve_count") or 0) != MAX_STATIONS
        or curves.get("all_complete") is not True
    ):
        raise AifsCandidateDecodeError("aifs_candidate_daily_curve_incomplete")
    curve_rows = curves.get("curves")
    if not isinstance(curve_rows, list) or {str(row.get("station_id")) for row in curve_rows if isinstance(row, Mapping)} != set(station_timezones):
        raise AifsCandidateDecodeError("aifs_candidate_daily_curve_station_scope_invalid")
    eligible = curves.get("eligible_model_rows")
    if not isinstance(eligible, list) or len(eligible) != MAX_STATIONS:
        raise AifsCandidateDecodeError("aifs_candidate_daily_candidate_row_count_invalid")
    raw_groups: dict[str, dict[str, Any]] = {}
    for arrival in arrivals:
        raw_sha = str(arrival["raw_message_sha256"])
        group = raw_groups.setdefault(raw_sha, {
            "raw_message_sha256": raw_sha,
            "raw_path": str(arrival["raw_path"]),
            "raw_bytes": int(arrival["raw_bytes"]),
            "raw_soft_limit_exceeded": bool(arrival["raw_soft_limit_exceeded"]),
            "source_url": str(arrival["source_url"]),
            "published_at_utc": str(arrival["published_at_utc"]),
            "received_at_utc": str(arrival["received_at_utc"]),
            "transport_receipt_sha256s": set(),
            "station_reference_count": 0,
            "valid_time_utc": str(arrival["valid_time_utc"]),
        })
        group["transport_receipt_sha256s"].add(str(arrival["transport_receipt_sha256"]))
        group["station_reference_count"] += 1
    raw_objects = [
        {
            **{key: value for key, value in row.items() if key != "transport_receipt_sha256s"},
            "transport_receipt_sha256s": sorted(row["transport_receipt_sha256s"]),
        }
        for _, row in sorted(raw_groups.items())
    ]
    total_raw_bytes = sum(int(item["raw_bytes"]) for item in raw_objects)
    candidate_daily_max_rows = [
        {
            **dict(row),
            "source_role": "official_native",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": True,
            "p1_or_hard_no_eligible": False,
        }
        for row in eligible
    ]
    identity = {
        "schema": SCHEMA,
        "state": "OFFICIAL_NATIVE_AIFS_SINGLE_45_STATION_CANDIDATE_COMPLETE",
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "model_id": "ECMWF",
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "target_local_date": str(capture["target_local_date"]),
        "run_initialization_utc": _timestamp_text(_timestamp("capture_run_initialization_utc", capture["run_initialization_utc"])),
        "decision_at_utc": _timestamp_text(_timestamp("capture_execution_decision_at_utc", capture["execution_decision_at_utc"])),
        "capture_evidence": {
            "path": str(_resolved_regular_file(capture_path, code="aifs_candidate_capture_missing")),
            "sha256": capture_sha,
            "content_sha256": str(capture["content_sha256"]),
            "fanout_station_arrival_count": len(arrivals),
        },
        "station_coordinate_registry": coordinate_registry_manifest(),
        "station_timezones": station_timezones,
        "raw_objects": raw_objects,
        "resource_bounds": {
            "raw_object_soft_limit_bytes": RAW_OBJECT_SOFT_LIMIT_BYTES,
            "raw_object_hard_limit_bytes": RAW_OBJECT_HARD_LIMIT_BYTES,
            "raw_total_hard_limit_bytes": RAW_TOTAL_HARD_LIMIT_BYTES,
            "raw_total_bytes": total_raw_bytes,
            "max_process_rss_bytes": MAX_PROCESS_RSS_BYTES,
            "minimum_free_bytes": MIN_FREE_BYTES,
            "max_decode_workers": MAX_WORKERS,
            "decoder_temporary_payload": "none_for_uncompressed_grib2",
            "raw_objects_reused": True,
            "network_requests": 0,
        },
        "authority": {
            "source_role": "official_native",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "strict_oos_activation_present": False,
            "counts_as_independent_family_from_ifs": False,
            "native_arrival_index_eligible": True,
            "p1_or_hard_no_eligible": False,
            "deterministic_single_not_ensemble": True,
            "provider_binding_required_before_ecmwf_role_selection": True,
        },
        "aifs_ensemble_variants": _aifs_ensemble_variant_lineage(),
        "temperature_points": points,
        "curves": curves,
        "candidate_daily_max_rows": candidate_daily_max_rows,
        "checks": {
            "canonical_station_count": MAX_STATIONS,
            "station_arrival_count": len(arrivals),
            "decoded_point_count": len(points),
            "complete_curve_count": int(curves["complete_curve_count"]),
            "all_station_curves_complete": bool(curves["all_complete"]),
            "all_raw_objects_official_native": True,
            "all_station_arrivals_prospective": True,
            "raw_objects_reused_without_download": True,
            "open_meteo_aifs_variants_not_upgraded": True,
            "p0_activation_created": False,
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "boundary": {
            "target_only": True,
            "read_only_decode": True,
            "network_requests": 0,
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "promotion_allowed": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--grib-get")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        evidence = build_candidate_evidence(
            args.capture.expanduser().resolve(strict=True),
            grib_get_binary=args.grib_get,
            workers=args.workers,
        )
        wrote = write_bounded_json_if_changed(
            output,
            evidence,
            limit=MAX_EVIDENCE_OUTPUT_BYTES,
        )
        print(json.dumps({
            "state": evidence["state"],
            "output": str(output),
            "wrote": wrote,
            "station_arrival_count": evidence["checks"]["station_arrival_count"],
            "decoded_point_count": evidence["checks"]["decoded_point_count"],
            "complete_curve_count": evidence["checks"]["complete_curve_count"],
            "formal_p0_eligible": False,
            "network_requests": 0,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (AifsCandidateDecodeError, ResourceBoundError, OSError, ValueError) as exc:
        blocked = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "provider_id": PROVIDER_ID,
            "blockers": [f"{type(exc).__name__}:{exc}"],
            "formal_p0_eligible": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        write_bounded_json_if_changed(output, blocked, limit=MAX_EVIDENCE_OUTPUT_BYTES)
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
