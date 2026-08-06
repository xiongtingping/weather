"""Build explicit historical native-ensemble OOS inputs from capture packets.

This is deliberately a pure adapter.  It neither writes the SSOT nor opens a
truth/holdout partition.  Its job is to preserve the exact member-set input a
later canonical ingress will need, without compressing a multi-object member
set into a fictitious singular raw-object field.

The only object-time evidence available in these archive packets is the
official HTTP ``Last-Modified`` header.  It is retained exclusively as an
``official_object_available_by_utc`` upper-bound proof.  The later archive
query remains a separate timestamp and does not change that meaning.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
from typing import Any

from .contracts import ContractError, content_sha256
from .model_product_identity import PRODUCT_BY_ID
from .native_ensemble_oos_contract import (
    AVAILABILITY_EVIDENCE_KIND,
    EVENT_FIELDS,
    EVENT_OOS_PROTOCOL_VERSION,
    EVENT_STATE,
    LEAD_BUCKET_SCOPE,
    RUN_CYCLE_SCOPE,
    CanonicalNativeEnsembleOOSEventFact,
)


SCHEMA = "native_ensemble_oos_event_input_v1"
MODEL_INPUT_KIND = "NATIVE_ENSEMBLE_MEMBER_SET"
SETTLEMENT_TIMEZONE = "America/New_York"
LEAD_BUCKET = "d2"
RUN_CYCLE = "18Z"
FORECAST_REDUCER = "ENSEMBLE_MEAN_OF_MEMBER_ET_DAY_MAX_V1"
OBJECT_TIME_EVIDENCE_KIND = AVAILABILITY_EVIDENCE_KIND


@dataclass(frozen=True)
class PacketSpec:
    packet_schema: str
    capture_schema: str
    product_id: str
    capture_independence_family_id: str
    expected_member_count: int
    horizon_key: str
    horizons: tuple[int, ...]
    extraction_version: str
    allowed_member_roles: frozenset[str]


PACKET_SPECS: dict[str, PacketSpec] = {
    "noaa_gefs_official_historical_multistation_members_v2": PacketSpec(
        packet_schema="noaa_gefs_official_historical_multistation_members_v2",
        capture_schema="noaa_gefs_official_historical_station_members_v2",
        product_id="NOAA_GEFS_05",
        capture_independence_family_id="NOAA_GEFS",
        expected_member_count=31,
        horizon_key="forecast_hour",
        horizons=(36, 39, 42, 45, 48, 51, 54, 57),
        extraction_version="noaa_gefs_pds_0p50_18z_d2_et_member_tmax_v1",
        allowed_member_roles=frozenset({"CONTROL", "PERTURBED"}),
    ),
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1": PacketSpec(
        packet_schema="ecmwf_ifs_open_pf50_historical_multistation_members_v1",
        capture_schema="ecmwf_ifs_open_pf50_historical_station_members_v1",
        product_id="ECMWF_IFS_ENS_OPEN_PF50",
        capture_independence_family_id="ECMWF",
        expected_member_count=50,
        horizon_key="forecast_step",
        horizons=(36, 39, 42, 45, 48, 51, 54, 57),
        extraction_version="ecmwf_open_data_ifs_0p25_enfo_pf50_18z_d2_et_member_tmax_v1",
        allowed_member_roles=frozenset({"PERTURBED"}),
    ),
}


def _product(spec: PacketSpec) -> Mapping[str, Any]:
    """Resolve output identity only from the canonical product registry."""

    product = PRODUCT_BY_ID.get(spec.product_id)
    if (
        product is None
        or product.get("product_id") != spec.product_id
        or product.get("ensemble_system_id") != spec.product_id
        or product.get("model_family") != "global_ensemble"
        or product.get("source_class") != "native_official"
    ):
        raise ContractError("native_oos_product_registry_mapping_invalid")
    return product


def _text(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(code)
    return result


def _sha(value: Any, code: str) -> str:
    result = _text(value, code).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ContractError(code)
    return result


def _finite(value: Any, code: str) -> float:
    if isinstance(value, bool):
        raise ContractError(code)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if not math.isfinite(result):
        raise ContractError(code)
    return result


def _utc(value: Any, code: str) -> tuple[str, datetime]:
    raw = _text(value, code)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z"), parsed


def _event_date(value: Any, code: str) -> date:
    try:
        return date.fromisoformat(_text(value, code))
    except ValueError as exc:
        raise ContractError(code) from exc


def _verify_content_hash(raw: Mapping[str, Any], code: str) -> str:
    supplied = _sha(raw.get("content_sha256"), code)
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(code + "_mismatch")
    return supplied


def _http_last_modified(value: Any, code: str) -> str:
    raw = _text(value, code)
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError) as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _assert_packet_execution_inert(raw: Mapping[str, Any], code: str) -> None:
    if raw.get("training_only") is not True:
        raise ContractError(code + "_training_only_invalid")
    if raw.get("holdout_eligible") is not False:
        raise ContractError(code + "_holdout_eligible_invalid")
    if raw.get("calls_order_gateway") is not False:
        raise ContractError(code + "_gateway_authority_invalid")
    if raw.get("live_submit_authorized") is not False:
        raise ContractError(code + "_live_authority_invalid")
    if raw.get("orders_submitted") != 0 or isinstance(raw.get("orders_submitted"), bool):
        raise ContractError(code + "_orders_invalid")


def _require_packet_scope(
    packet: Mapping[str, Any], spec: PacketSpec
) -> tuple[str, date, datetime, str]:
    packet_sha = _verify_content_hash(packet, "native_oos_packet_content_sha_invalid")
    if _text(packet.get("schema"), "native_oos_packet_schema_missing") != spec.packet_schema:
        raise ContractError("native_oos_packet_schema_mismatch")
    for field, expected in (
        ("ensemble_system_id", spec.product_id),
        ("independence_family_id", spec.capture_independence_family_id),
        ("run_cycle", RUN_CYCLE),
    ):
        if _text(packet.get(field), "native_oos_packet_scope_missing:" + field) != expected:
            raise ContractError("native_oos_packet_scope_mismatch:" + field)
    _assert_packet_execution_inert(packet, "native_oos_packet")
    target_day = _event_date(packet.get("event_date"), "native_oos_packet_event_date_invalid")
    run_init, run_init_dt = _utc(packet.get("run_init_utc"), "native_oos_packet_run_init_invalid")
    expected_run_init = datetime.combine(
        target_day - timedelta(days=2), time(hour=18), timezone.utc
    )
    if run_init_dt != expected_run_init:
        raise ContractError("native_oos_packet_d2_18z_run_init_mismatch")
    archive_query, archive_query_dt = _utc(
        packet.get("archive_query_at_utc"), "native_oos_packet_archive_query_invalid"
    )
    if archive_query_dt <= run_init_dt:
        raise ContractError("native_oos_packet_archive_query_before_run")
    return packet_sha, target_day, run_init_dt, archive_query


def _source_receipt_evidence(
    receipt: Mapping[str, Any], *, spec: PacketSpec, member_id: str
) -> dict[str, Any]:
    if _text(receipt.get("member_id"), "native_oos_receipt_member_missing") != member_id:
        raise ContractError("native_oos_receipt_member_mismatch")
    try:
        horizon = int(receipt.get(spec.horizon_key))
    except (TypeError, ValueError) as exc:
        raise ContractError("native_oos_receipt_horizon_invalid") from exc
    if horizon not in spec.horizons:
        raise ContractError("native_oos_receipt_horizon_invalid")
    object_key = _text(receipt.get("object_key"), "native_oos_receipt_object_key_missing")
    message_sha = _sha(receipt.get("message_sha256"), "native_oos_receipt_message_sha_invalid")
    index_sha = _sha(receipt.get("index_sha256"), "native_oos_receipt_index_sha_invalid")
    if spec.product_id == "ECMWF_IFS_ENS_OPEN_PF50":
        _sha(receipt.get("index_row_sha256"), "native_oos_receipt_index_row_sha_invalid")
    else:
        _text(receipt.get("index_line"), "native_oos_receipt_index_line_missing")
    for field in ("range_start", "range_end"):
        value = receipt.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError("native_oos_receipt_range_invalid:" + field)
    if int(receipt["range_start"]) >= int(receipt["range_end"]):
        raise ContractError("native_oos_receipt_range_order_invalid")
    identity = {
        "member_id": member_id,
        "lead_time_hours": horizon,
        "raw_object_id": object_key,
        "raw_message_sha256": message_sha,
        "index_sha256": index_sha,
        "range_start": int(receipt["range_start"]),
        "range_end": int(receipt["range_end"]),
        "official_object_available_by_utc": _http_last_modified(
            receipt.get("last_modified"), "native_oos_receipt_last_modified_invalid"
        ),
        "official_object_available_by_utc_evidence_kind": OBJECT_TIME_EVIDENCE_KIND,
    }
    if spec.product_id == "ECMWF_IFS_ENS_OPEN_PF50":
        identity["index_row_sha256"] = _sha(
            receipt.get("index_row_sha256"), "native_oos_receipt_index_row_sha_invalid"
        )
    else:
        identity["index_line"] = _text(
            receipt.get("index_line"), "native_oos_receipt_index_line_missing"
        )
    return {
        **identity,
        "source_field_receipt_content_sha256": content_sha256(identity),
    }


def _member_evidence(
    member: Mapping[str, Any],
    *,
    receipts: Sequence[Mapping[str, Any]],
    spec: PacketSpec,
    capture_sha: str,
    archive_query_at_utc: str,
) -> dict[str, Any]:
    member_id = _text(member.get("member_id"), "native_oos_member_id_missing")
    member_role = _text(member.get("member_role"), "native_oos_member_role_missing")
    if member_role not in spec.allowed_member_roles:
        raise ContractError("native_oos_member_role_invalid")
    if _text(member.get("ensemble_system_id"), "native_oos_member_system_missing") != spec.product_id:
        raise ContractError("native_oos_member_system_mismatch")
    member_value = _finite(member.get("raw_tmax_c"), "native_oos_member_et_day_max_invalid")
    source_receipts = sorted(
        (
            _source_receipt_evidence(row, spec=spec, member_id=member_id)
            for row in receipts
        ),
        key=lambda row: int(row["lead_time_hours"]),
    )
    if len(source_receipts) != len(spec.horizons):
        raise ContractError("native_oos_member_receipt_count_incomplete")
    if tuple(row["lead_time_hours"] for row in source_receipts) != spec.horizons:
        raise ContractError("native_oos_member_receipt_horizons_incomplete")
    receipt_set_sha = content_sha256(source_receipts)
    available_by = max(
        str(row["official_object_available_by_utc"]) for row in source_receipts
    )
    identity = {
        "member_id": member_id,
        "member_role": member_role,
        "member_et_day_max_c": member_value,
        "field_receipt_count": len(source_receipts),
        "field_receipts": source_receipts,
        "field_receipt_set_sha256": receipt_set_sha,
        "official_object_available_by_utc": available_by,
        "official_object_available_by_utc_evidence_kind": OBJECT_TIME_EVIDENCE_KIND,
        "archive_query_at_utc": archive_query_at_utc,
        "source_capture_content_sha256": capture_sha,
    }
    return {
        **identity,
        "source_member_evidence_sha256": content_sha256(identity),
        "field_receipt_manifest_sha256": receipt_set_sha,
    }


def _validate_capture(
    capture: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
    packet_target_day: date,
    packet_run_init: datetime,
    packet_archive_query: str,
    packet_sha: str,
    source_packet_ref: str,
    spec: PacketSpec,
) -> dict[str, Any]:
    product = _product(spec)
    capture_sha = _verify_content_hash(capture, "native_oos_capture_content_sha_invalid")
    _assert_packet_execution_inert(capture, "native_oos_capture")
    if _text(capture.get("schema"), "native_oos_capture_schema_missing") != spec.capture_schema:
        raise ContractError("native_oos_capture_schema_mismatch")
    for field, expected in (
        ("event_date", packet_target_day.isoformat()),
        ("run_cycle", RUN_CYCLE),
        ("ensemble_system_id", spec.product_id),
        ("independence_family_id", spec.capture_independence_family_id),
        ("settlement_timezone", SETTLEMENT_TIMEZONE),
        ("source_class", "OFFICIAL_NATIVE_ARCHIVE"),
    ):
        if _text(capture.get(field), "native_oos_capture_scope_missing:" + field) != expected:
            raise ContractError("native_oos_capture_scope_mismatch:" + field)
    capture_run_init, capture_run_init_dt = _utc(
        capture.get("run_init_utc"), "native_oos_capture_run_init_invalid"
    )
    if capture_run_init_dt != packet_run_init:
        raise ContractError("native_oos_capture_run_init_mismatch")
    capture_archive_query, _capture_archive_query_dt = _utc(
        capture.get("archive_query_at_utc"), "native_oos_capture_archive_query_invalid"
    )
    if capture_archive_query != packet_archive_query:
        raise ContractError("native_oos_capture_archive_query_mismatch")
    if capture.get("native_run_init_known") is not True:
        raise ContractError("native_oos_capture_run_init_evidence_invalid")
    if capture.get("native_publication_at_known") is not False:
        raise ContractError("native_oos_capture_object_time_claim_invalid")

    station_id = _text(capture.get("station_id"), "native_oos_capture_station_missing").upper()
    members_raw = capture.get("members")
    receipts_raw = capture.get("field_receipts")
    if not isinstance(members_raw, Sequence) or isinstance(members_raw, (str, bytes)):
        raise ContractError("native_oos_capture_members_invalid")
    if not isinstance(receipts_raw, Sequence) or isinstance(receipts_raw, (str, bytes)):
        raise ContractError("native_oos_capture_receipts_invalid")
    if int(capture.get("member_count") or 0) != spec.expected_member_count:
        raise ContractError("native_oos_capture_member_count_invalid")
    if len(members_raw) != spec.expected_member_count:
        raise ContractError("native_oos_capture_member_count_incomplete")
    if int(capture.get("field_receipt_count") or 0) != spec.expected_member_count * len(spec.horizons):
        raise ContractError("native_oos_capture_receipt_count_invalid")
    if len(receipts_raw) != spec.expected_member_count * len(spec.horizons):
        raise ContractError("native_oos_capture_receipt_count_incomplete")
    if _sha(capture.get("field_receipt_set_sha256"), "native_oos_capture_receipt_set_sha_invalid") != content_sha256(receipts_raw):
        raise ContractError("native_oos_capture_receipt_set_sha_mismatch")

    members: list[Mapping[str, Any]] = []
    member_ids: set[str] = set()
    for raw in members_raw:
        if not isinstance(raw, Mapping):
            raise ContractError("native_oos_capture_member_mapping_required")
        member_id = _text(raw.get("member_id"), "native_oos_member_id_missing")
        if member_id in member_ids:
            raise ContractError("native_oos_member_id_duplicate")
        member_ids.add(member_id)
        members.append(raw)
    member_identity = [
        {
            "member_id": _text(row.get("member_id"), "native_oos_member_id_missing"),
            "member_role": _text(row.get("member_role"), "native_oos_member_role_missing"),
            "raw_tmax_c": _finite(row.get("raw_tmax_c"), "native_oos_member_et_day_max_invalid"),
        }
        for row in members
    ]
    if _sha(capture.get("member_set_sha256"), "native_oos_capture_member_set_sha_invalid") != content_sha256(member_identity):
        raise ContractError("native_oos_capture_member_set_sha_mismatch")

    receipts_by_member: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in receipts_raw:
        if not isinstance(raw, Mapping):
            raise ContractError("native_oos_capture_receipt_mapping_required")
        receipt_member_id = _text(raw.get("member_id"), "native_oos_receipt_member_missing")
        if receipt_member_id not in member_ids:
            raise ContractError("native_oos_receipt_member_unknown")
        receipts_by_member[receipt_member_id].append(raw)
    if set(receipts_by_member) != member_ids:
        raise ContractError("native_oos_capture_member_receipts_incomplete")

    member_evidence = [
        _member_evidence(
            member,
            receipts=receipts_by_member[_text(member.get("member_id"), "native_oos_member_id_missing")],
            spec=spec,
            capture_sha=capture_sha,
            archive_query_at_utc=capture_archive_query,
        )
        for member in members
    ]
    member_evidence.sort(key=lambda row: str(row["member_id"]))
    member_evidence_set_sha = content_sha256(member_evidence)
    member_evidence_vector = [
        {
            "member_id": str(row["member_id"]),
            "role": str(row["member_role"]),
            "value_c": float(row["member_et_day_max_c"]),
            "source_member_evidence_sha256": str(
                row["source_member_evidence_sha256"]
            ),
            "field_receipt_manifest_sha256": str(
                row["field_receipt_manifest_sha256"]
            ),
        }
        for row in member_evidence
    ]
    member_evidence_vector_sha = content_sha256(member_evidence_vector)
    forecast_value = math.fsum(
        float(row["member_et_day_max_c"]) for row in member_evidence
    ) / len(member_evidence)
    available_by = max(
            str(row["official_object_available_by_utc"]) for row in member_evidence
    )
    raw_message_sha256s = sorted({
        str(receipt["raw_message_sha256"])
        for row in member_evidence
        for receipt in row["field_receipts"]
    })
    raw_object_ids = sorted({
        str(receipt["raw_object_id"])
        for row in member_evidence
        for receipt in row["field_receipts"]
    })
    event_input_id = "|".join(
        (
            str(product["ensemble_system_id"]),
            station_id,
            packet_target_day.isoformat(),
            capture_run_init,
            LEAD_BUCKET,
            RUN_CYCLE,
        )
    )
    identity = {
        "schema": SCHEMA,
        "event_input_id": event_input_id,
        "model_input_kind": MODEL_INPUT_KIND,
        "station_id": station_id,
        "event_date": packet_target_day.isoformat(),
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "model_id": product["model_id"],
        "ensemble_system_id": product["ensemble_system_id"],
        "model_family": product["model_family"],
        "provider_id": product["provider_id"],
        "provider_product_id": product["provider_product_id"],
        "independence_family_id": product["independence_family_id"],
        "source_class": product["source_class"],
        "run_init_utc": capture_run_init,
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "extraction_version": spec.extraction_version,
        "forecast_reducer": FORECAST_REDUCER,
        "forecast_value": forecast_value,
        "expected_member_count": spec.expected_member_count,
        "member_count": len(member_evidence),
        "member_evidence": member_evidence,
        "member_evidence_set_sha256": member_evidence_set_sha,
        "member_evidence_vector": member_evidence_vector,
        "member_evidence_vector_sha256": member_evidence_vector_sha,
        "raw_message_sha256s": raw_message_sha256s,
        "raw_message_vector_sha256": content_sha256(raw_message_sha256s),
        "raw_object_ids": raw_object_ids,
        "raw_object_id_vector_sha256": content_sha256(raw_object_ids),
        "official_object_available_by_utc": available_by,
        "official_object_available_by_utc_evidence_kind": OBJECT_TIME_EVIDENCE_KIND,
        "archive_query_at_utc": capture_archive_query,
        "source_capture_content_sha256": capture_sha,
        "source_packet_content_sha256": packet_sha,
        "source_packet_ref": source_packet_ref,
        "oos_evaluation_state": "PENDING_EXACT_MASK_RESERVATION",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_native_ensemble_oos_event_inputs(
    packet: Mapping[str, Any],
    *,
    source_packet_ref: str | Path | None = None,
    station_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return explicit member-set inputs for the requested packet captures.

    The returned ordinary dictionaries are intentionally not canonical SSOT
    facts yet.  Reservation, truth access, and OOS evaluation remain later,
    separate phases. ``station_ids`` is an exact, fail-closed projection of
    an already sealed multistation packet; the source packet and capture
    hashes remain unchanged.
    """

    if not isinstance(packet, Mapping):
        raise ContractError("native_oos_packet_mapping_required")
    if source_packet_ref is None:
        raise ContractError("native_oos_source_packet_absolute_path_required")
    supplied_path = Path(source_packet_ref)
    if not supplied_path.is_absolute():
        raise ContractError("native_oos_source_packet_absolute_path_required")
    try:
        packet_path = supplied_path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("native_oos_source_packet_path_missing") from exc
    if not packet_path.is_file():
        raise ContractError("native_oos_source_packet_path_not_file")
    try:
        file_packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("native_oos_source_packet_file_invalid") from exc
    if not isinstance(file_packet, Mapping) or dict(file_packet) != dict(packet):
        raise ContractError("native_oos_source_packet_mapping_mismatch")
    schema = _text(packet.get("schema"), "native_oos_packet_schema_missing")
    spec = PACKET_SPECS.get(schema)
    if spec is None:
        raise ContractError("native_oos_packet_schema_unsupported")
    packet_sha, target_day, run_init, archive_query = _require_packet_scope(packet, spec)
    captures = packet.get("captures")
    if not isinstance(captures, Sequence) or isinstance(captures, (str, bytes)):
        raise ContractError("native_oos_packet_captures_invalid")
    if int(packet.get("station_count") or 0) != len(captures) or not captures:
        raise ContractError("native_oos_packet_station_count_invalid")
    packet_station_ids = packet.get("station_ids")
    if not isinstance(packet_station_ids, list) or packet_station_ids != sorted(
        {
            _text(value, "native_oos_packet_station_id_invalid").upper()
            for value in packet_station_ids
        }
    ):
        raise ContractError("native_oos_packet_station_ids_invalid")
    if len(packet_station_ids) != len(captures):
        raise ContractError("native_oos_packet_station_ids_count_invalid")
    requested_station_ids = station_ids
    if requested_station_ids is None:
        requested = list(packet_station_ids)
    else:
        requested = sorted({
            _text(value, "native_oos_projection_station_invalid").upper()
            for value in requested_station_ids
        })
        if (
            not requested
            or len(requested) != len(requested_station_ids)
            or not set(requested) <= set(packet_station_ids)
        ):
            raise ContractError("native_oos_projection_station_scope_invalid")
    selected_captures = [
        capture for capture in captures
        if isinstance(capture, Mapping)
        and _text(
            capture.get("station_id"),
            "native_oos_capture_station_missing",
        ).upper() in requested
    ]
    events = [
        _validate_capture(
            capture,
            packet=packet,
            packet_target_day=target_day,
            packet_run_init=run_init,
            packet_archive_query=archive_query,
            packet_sha=packet_sha,
            source_packet_ref=str(packet_path),
            spec=spec,
        )
        for capture in selected_captures
    ]
    if len(events) != len(requested):
        raise ContractError("native_oos_projection_capture_count_invalid")
    if [str(row["station_id"]) for row in sorted(events, key=lambda row: str(row["station_id"]))] != requested:
        raise ContractError("native_oos_packet_capture_station_ids_mismatch")
    events.sort(key=lambda row: str(row["station_id"]))
    return events


def validate_native_ensemble_oos_event_input(event: Mapping[str, Any]) -> None:
    """Verify an adapter output can be content-addressed again by an ingress."""

    if not isinstance(event, Mapping):
        raise ContractError("native_oos_event_mapping_required")
    if _text(event.get("schema"), "native_oos_event_schema_missing") != SCHEMA:
        raise ContractError("native_oos_event_schema_invalid")
    if _text(event.get("model_input_kind"), "native_oos_event_input_kind_missing") != MODEL_INPUT_KIND:
        raise ContractError("native_oos_event_input_kind_invalid")
    if "raw_object_sha256" in event:
        raise ContractError("native_oos_event_singular_raw_object_forbidden")
    product = PRODUCT_BY_ID.get(
        _text(event.get("ensemble_system_id"), "native_oos_event_system_missing")
    )
    if product is None or any(
        event.get(field) != product.get(field)
        for field in (
            "model_id",
            "ensemble_system_id",
            "model_family",
            "provider_id",
            "provider_product_id",
            "independence_family_id",
            "source_class",
        )
    ):
        raise ContractError("native_oos_event_product_registry_mismatch")
    identity = {key: value for key, value in event.items() if key != "content_sha256"}
    if _sha(event.get("content_sha256"), "native_oos_event_content_sha_invalid") != content_sha256(identity):
        raise ContractError("native_oos_event_content_sha_mismatch")
    member_evidence = event.get("member_evidence")
    if not isinstance(member_evidence, list) or not member_evidence:
        raise ContractError("native_oos_event_member_evidence_invalid")
    if _sha(event.get("member_evidence_set_sha256"), "native_oos_event_member_set_sha_invalid") != content_sha256(member_evidence):
        raise ContractError("native_oos_event_member_set_sha_mismatch")
    for member in member_evidence:
        if not isinstance(member, Mapping):
            raise ContractError("native_oos_event_member_evidence_mapping_required")
        member_identity = {
            key: value
            for key, value in member.items()
            if key not in {
                "source_member_evidence_sha256",
                "field_receipt_manifest_sha256",
            }
        }
        if _sha(
            member.get("source_member_evidence_sha256"),
            "native_oos_event_member_evidence_sha_invalid",
        ) != content_sha256(member_identity):
            raise ContractError("native_oos_event_member_evidence_sha_mismatch")
        receipts = member.get("field_receipts")
        if not isinstance(receipts, list) or not receipts:
            raise ContractError("native_oos_event_member_receipts_invalid")
        if _sha(member.get("field_receipt_set_sha256"), "native_oos_event_member_receipt_set_sha_invalid") != content_sha256(receipts):
            raise ContractError("native_oos_event_member_receipt_set_sha_mismatch")
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                raise ContractError("native_oos_event_receipt_mapping_required")
            receipt_identity = {
                key: value
                for key, value in receipt.items()
                if key != "source_field_receipt_content_sha256"
            }
            if _sha(
                receipt.get("source_field_receipt_content_sha256"),
                "native_oos_event_receipt_sha_invalid",
            ) != content_sha256(receipt_identity):
                raise ContractError("native_oos_event_receipt_sha_mismatch")
    vector = event.get("member_evidence_vector")
    if not isinstance(vector, list) or vector != [
        {
            "member_id": str(member["member_id"]),
            "role": str(member["member_role"]),
            "value_c": float(member["member_et_day_max_c"]),
            "source_member_evidence_sha256": str(
                member["source_member_evidence_sha256"]
            ),
            "field_receipt_manifest_sha256": str(
                member["field_receipt_manifest_sha256"]
            ),
        }
        for member in member_evidence
    ]:
        raise ContractError("native_oos_event_member_vector_mismatch")
    if _sha(
        event.get("member_evidence_vector_sha256"),
        "native_oos_event_member_vector_sha_invalid",
    ) != content_sha256(vector):
        raise ContractError("native_oos_event_member_vector_sha_mismatch")
    raw_message_sha256s = event.get("raw_message_sha256s")
    raw_object_ids = event.get("raw_object_ids")
    if (
        not isinstance(raw_message_sha256s, list)
        or raw_message_sha256s != sorted(set(raw_message_sha256s))
        or any(_sha(value, "native_oos_event_message_sha_invalid") != value for value in raw_message_sha256s)
        or _sha(
            event.get("raw_message_vector_sha256"),
            "native_oos_event_message_vector_sha_invalid",
        ) != content_sha256(raw_message_sha256s)
    ):
        raise ContractError("native_oos_event_message_vector_invalid")
    if (
        not isinstance(raw_object_ids, list)
        or not raw_object_ids
        or raw_object_ids != sorted(set(raw_object_ids))
        or any(not isinstance(value, str) or not value for value in raw_object_ids)
        or _sha(
            event.get("raw_object_id_vector_sha256"),
            "native_oos_event_object_id_vector_sha_invalid",
        ) != content_sha256(raw_object_ids)
    ):
        raise ContractError("native_oos_event_object_id_vector_invalid")


def build_native_ensemble_contract_event_identity(
    event_input: Mapping[str, Any], exact_truth: Mapping[str, Any]
) -> dict[str, Any]:
    """Join a complete adapter output to an already exact truth mapping.

    This function is intentionally pure and requires every contract clock and
    truth field explicitly.  It emits object *identifiers*, not imaginary
    object digests, so a later contract revision can consume the vector
    without changing its provenance meaning.
    """

    validate_native_ensemble_oos_event_input(event_input)
    if not isinstance(exact_truth, Mapping):
        raise ContractError("native_oos_exact_truth_mapping_required")
    required_truth_fields = {
        "station_id",
        "event_date",
        "forecast_decision_at_utc",
        "canonical_truth_fact_content_sha256",
        "truth_id",
        "truth_revision_no",
        "truth_content_sha256",
        "truth_value_c",
        "station_day_end_utc",
        "truth_canonical_committed_at_utc",
    }
    if set(exact_truth) != required_truth_fields:
        raise ContractError("native_oos_exact_truth_fields_mismatch")
    station_id = _text(exact_truth.get("station_id"), "native_oos_exact_truth_station_missing").upper()
    event_date = _event_date(
        exact_truth.get("event_date"), "native_oos_exact_truth_event_date_invalid"
    ).isoformat()
    if station_id != event_input["station_id"] or event_date != event_input["event_date"]:
        raise ContractError("native_oos_exact_truth_scope_mismatch")
    forecast_decision_at_utc, forecast_decision_at = _utc(
        exact_truth.get("forecast_decision_at_utc"),
        "native_oos_exact_truth_decision_at_invalid",
    )
    station_day_end_utc, station_day_end = _utc(
        exact_truth.get("station_day_end_utc"),
        "native_oos_exact_truth_day_end_invalid",
    )
    truth_committed_at_utc, truth_committed_at = _utc(
        exact_truth.get("truth_canonical_committed_at_utc"),
        "native_oos_exact_truth_committed_at_invalid",
    )
    available_by_utc, available_by = _utc(
        event_input.get("official_object_available_by_utc"),
        "native_oos_event_available_by_invalid",
    )
    run_init_utc, run_init = _utc(
        event_input.get("run_init_utc"), "native_oos_event_run_init_invalid"
    )
    if not (run_init <= available_by <= forecast_decision_at < station_day_end <= truth_committed_at):
        raise ContractError("native_oos_exact_truth_temporal_order_invalid")
    if isinstance(exact_truth.get("truth_revision_no"), bool) or not isinstance(
        exact_truth.get("truth_revision_no"), int
    ) or int(exact_truth["truth_revision_no"]) < 0:
        raise ContractError("native_oos_exact_truth_revision_invalid")
    identity = {
        "station_id": station_id,
        "model_id": event_input["model_id"],
        "ensemble_system_id": event_input["ensemble_system_id"],
        "model_family": event_input["model_family"],
        "provider_id": event_input["provider_id"],
        "provider_product_id": event_input["provider_product_id"],
        "independence_family_id": event_input["independence_family_id"],
        "source_class": event_input["source_class"],
        "authority_scope": event_input["authority_scope"],
        "event_date": event_date,
        "settlement_timezone": event_input["settlement_timezone"],
        "lead_bucket": event_input["lead_bucket"],
        "lead_bucket_scope": LEAD_BUCKET_SCOPE,
        "run_init_utc": run_init_utc,
        "run_cycle": event_input["run_cycle"],
        "run_cycle_scope": RUN_CYCLE_SCOPE,
        "forecast_decision_at_utc": forecast_decision_at_utc,
        "official_object_available_by_utc": available_by_utc,
        "availability_evidence_kind": OBJECT_TIME_EVIDENCE_KIND,
        "native_arrival_indexed": False,
        "formal_live_authority": False,
        "p1_eligible": False,
        "member_count": event_input["member_count"],
        "daily_max_extraction_version": event_input["extraction_version"],
        "ensemble_reducer_version": event_input["forecast_reducer"],
        "forecast_value_c": event_input["forecast_value"],
        "member_evidence_vector": event_input["member_evidence_vector"],
        "member_evidence_vector_sha256": event_input[
            "member_evidence_vector_sha256"
        ],
        "raw_message_sha256s": event_input["raw_message_sha256s"],
        "raw_message_vector_sha256": event_input["raw_message_vector_sha256"],
        "raw_object_ids": event_input["raw_object_ids"],
        "raw_object_id_vector_sha256": event_input[
            "raw_object_id_vector_sha256"
        ],
        "source_packet_sha256": event_input["source_packet_content_sha256"],
        "source_packet_ref": event_input["source_packet_ref"],
        "adapter_event_input_content_sha256": event_input["content_sha256"],
        "archive_retrieved_at_utc": event_input["archive_query_at_utc"],
        "canonical_truth_fact_content_sha256": _sha(
            exact_truth.get("canonical_truth_fact_content_sha256"),
            "native_oos_exact_truth_fact_sha_invalid",
        ),
        "truth_id": _text(exact_truth.get("truth_id"), "native_oos_exact_truth_id_missing"),
        "truth_revision_no": int(exact_truth["truth_revision_no"]),
        "truth_content_sha256": _sha(
            exact_truth.get("truth_content_sha256"),
            "native_oos_exact_truth_content_sha_invalid",
        ),
        "truth_value_c": _finite(
            exact_truth.get("truth_value_c"), "native_oos_exact_truth_value_invalid"
        ),
        "station_day_end_utc": station_day_end_utc,
        "truth_canonical_committed_at_utc": truth_committed_at_utc,
        "event_oos_protocol_version": EVENT_OOS_PROTOCOL_VERSION,
        "state": EVENT_STATE,
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    if set(result) != EVENT_FIELDS:
        raise ContractError("native_oos_contract_event_fields_mismatch")
    return dict(CanonicalNativeEnsembleOOSEventFact.from_mapping(result).raw)
