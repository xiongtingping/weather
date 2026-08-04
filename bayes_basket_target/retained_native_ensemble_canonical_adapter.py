"""Exact retained GEFS/GEPS member -> canonical candidate adapter.

The retained historical packets contain immutable decoded member values and
receipt-set hashes, but they do not contain independently verified native
publication/receipt timestamps.  This adapter therefore emits canonical
ensemble-member *candidates* only.  It never upgrades the legacy OOS flag to
``historical_strict_oos`` and never manufactures a native-arrival fact.

Every source member must agree with both its retained packet and the exact
legacy content-addressed row.  The legacy table is evidence for a one-time
migration, not a runtime fallback or a second writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .canonical_contract import CONTRACT_VERSION, SCHEMA
from .contracts import ContractError, content_sha256
from .model_system_coverage import canonical_station_ids


ADAPTER_SCHEMA = "retained_native_ensemble_canonical_candidate_adapter_v1"
RECEIPT_SCHEMA = "retained_native_ensemble_canonical_candidate_receipt_v1"
SOURCE_CLASS = "OFFICIAL_NATIVE_ARCHIVE"
CANONICAL_SOURCE_CLASS = "native_official"
CANONICAL_OOS_ROLE = "observational_candidate_not_oos"


@dataclass(frozen=True)
class SourceSpec:
    packet_schema: str
    ensemble_system_id: str
    independence_family_id: str
    provider_id: str
    transport_provider_id: str
    lead_bucket: str
    run_cycle: str
    extraction_version: str
    expected_member_count: int
    raw_object_ref_prefix: str


SOURCE_SPECS = {
    "noaa_gefs_official_historical_multistation_members_v2": SourceSpec(
        packet_schema="noaa_gefs_official_historical_multistation_members_v2",
        ensemble_system_id="NOAA_GEFS_05",
        independence_family_id="NOAA_GEFS",
        provider_id="NOAA",
        transport_provider_id="NOAA_GEFS_PDS_S3",
        lead_bucket="d2",
        run_cycle="18Z",
        extraction_version="noaa_gefs_pds_0p50_18z_d2_station_tmax_v2",
        expected_member_count=31,
        raw_object_ref_prefix="gefs_field_receipt_set_sha256:",
    ),
    "eccc_geps_official_historical_multistation_members_v1": SourceSpec(
        packet_schema="eccc_geps_official_historical_multistation_members_v1",
        ensemble_system_id="ECCC_GEM_GLOBAL_ENS",
        independence_family_id="ECCC_GEM",
        provider_id="ECCC",
        transport_provider_id="ECCC_DD_WEATHER",
        lead_bucket="d2",
        run_cycle="00Z",
        extraction_version="eccc_geps_0p5_00z_d2_station_tmax_v1",
        expected_member_count=21,
        raw_object_ref_prefix="eccc_geps_file_receipt_set_sha256:",
    ),
}


LEGACY_IDENTITY_FIELDS = (
    "ensemble_system_id",
    "station_id",
    "target_local_date",
    "archive_lead_bucket",
    "run_cycle",
    "member_id",
    "member_role",
    "raw_tmax_c",
    "settlement_definition",
    "extraction_version",
    "provider_id",
    "transport_provider_id",
    "native_run_init_utc",
    "offline_oos_eligible",
    "archive_query_at_utc",
    "source_url",
    "raw_object_ref",
    "source_object_sha256",
    "qc_status",
    "authority_scope",
    "holdout_eligible",
)


def _text(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(code)
    return result


def _sha(value: Any, code: str) -> str:
    result = _text(value, code).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
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
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), parsed


def _verify_content_hash(raw: Mapping[str, Any], code: str) -> str:
    supplied = _sha(raw.get("content_sha256"), code)
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise ContractError(code + "_mismatch")
    return supplied


def _closed_day(
    *, event_date: str, settlement_timezone: str, as_of: datetime
) -> bool:
    try:
        zone = ZoneInfo(settlement_timezone)
        target = date.fromisoformat(event_date)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("retained_member_settlement_scope_invalid") from exc
    day_end = datetime.combine(target.fromordinal(target.toordinal() + 1), time(), zone)
    return as_of >= day_end.astimezone(timezone.utc)


def _legacy_key(
    system_id: str,
    station_id: str,
    event_date: str,
    lead_bucket: str,
    run_cycle: str,
    member_id: str,
) -> tuple[str, str, str, str, str, str]:
    return (
        system_id,
        station_id,
        event_date,
        lead_bucket,
        run_cycle,
        member_id,
    )


def _validate_legacy_row(
    raw: Mapping[str, Any],
    *,
    spec: SourceSpec,
    capture: Mapping[str, Any],
    member: Mapping[str, Any],
    receipt_set_sha: str,
) -> tuple[str, str]:
    missing = [field for field in (*LEGACY_IDENTITY_FIELDS, "content_sha256") if field not in raw]
    if missing:
        raise ContractError(
            "retained_member_legacy_field_missing:" + ",".join(sorted(missing))
        )
    identity = {field: raw[field] for field in LEGACY_IDENTITY_FIELDS}
    supplied = _sha(raw["content_sha256"], "retained_member_legacy_sha_invalid")
    if content_sha256(identity) != supplied:
        raise ContractError("retained_member_legacy_sha_mismatch")
    exact = {
        "ensemble_system_id": spec.ensemble_system_id,
        "station_id": _text(capture.get("station_id"), "retained_member_station_missing").upper(),
        "target_local_date": _text(capture.get("event_date"), "retained_member_event_date_missing"),
        "archive_lead_bucket": spec.lead_bucket,
        "run_cycle": spec.run_cycle,
        "member_id": _text(member.get("member_id"), "retained_member_id_missing"),
        "member_role": _text(member.get("member_role"), "retained_member_role_missing"),
        "raw_tmax_c": float(member.get("raw_tmax_c")),
        "settlement_definition": "official_daily_tmax_c",
        "extraction_version": spec.extraction_version,
        "provider_id": spec.provider_id,
        "transport_provider_id": spec.transport_provider_id,
        "native_run_init_utc": _text(capture.get("run_init_utc"), "retained_member_run_init_missing"),
        "offline_oos_eligible": 1,
        "source_url": _text(capture.get("source_prefix"), "retained_member_source_prefix_missing"),
        "raw_object_ref": spec.raw_object_ref_prefix + receipt_set_sha,
        "source_object_sha256": receipt_set_sha,
        "qc_status": "ACCEPTED",
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": 0,
    }
    for field, expected in exact.items():
        actual = identity[field]
        if field == "raw_tmax_c":
            try:
                matches = float(actual) == float(expected)
            except (TypeError, ValueError):
                matches = False
        elif field in {"offline_oos_eligible", "holdout_eligible"}:
            matches = actual == expected and not isinstance(actual, bool)
        else:
            matches = str(actual) == str(expected)
        if not matches:
            raise ContractError("retained_member_legacy_field_mismatch:" + field)
    legacy_archive_query, _legacy_archive_dt = _utc(
        identity["archive_query_at_utc"],
        "retained_member_legacy_archive_query_invalid",
    )
    return supplied, legacy_archive_query


def adapt_retained_packet_to_canonical_candidates(
    packet: Mapping[str, Any],
    *,
    legacy_rows: Mapping[tuple[str, str, str, str, str, str], Mapping[str, Any]],
    as_of_utc: str,
    builder_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one v2 canonical member-only contract and migration receipt."""

    if not isinstance(packet, Mapping):
        raise ContractError("retained_member_packet_mapping_required")
    schema = _text(packet.get("schema"), "retained_member_packet_schema_missing")
    spec = SOURCE_SPECS.get(schema)
    if spec is None:
        raise ContractError("retained_member_packet_schema_unsupported")
    packet_sha = _verify_content_hash(packet, "retained_member_packet_sha_invalid")
    as_of, as_of_dt = _utc(as_of_utc, "retained_member_as_of_invalid")
    if packet.get("calls_order_gateway") is not False:
        raise ContractError("retained_member_packet_gateway_authority_invalid")
    if packet.get("live_submit_authorized") is not False:
        raise ContractError("retained_member_packet_live_authority_invalid")
    if packet.get("training_only") is not True:
        raise ContractError("retained_member_packet_training_role_invalid")
    if int(packet.get("orders_submitted") or 0) != 0:
        raise ContractError("retained_member_packet_orders_invalid")
    for field, expected in (
        ("ensemble_system_id", spec.ensemble_system_id),
        ("independence_family_id", spec.independence_family_id),
        ("run_cycle", spec.run_cycle),
    ):
        if str(packet.get(field) or "") != expected:
            raise ContractError("retained_member_packet_scope_mismatch:" + field)
    packet_run_init, _run_init_dt = _utc(
        packet.get("run_init_utc"), "retained_member_packet_run_init_invalid"
    )
    packet_event_date = _text(
        packet.get("event_date"), "retained_member_packet_event_date_missing"
    )
    captures = packet.get("captures")
    if not isinstance(captures, list) or len(captures) != 45:
        raise ContractError("retained_member_packet_exact45_required")
    if int(packet.get("station_count") or -1) != 45:
        raise ContractError("retained_member_packet_station_count_invalid")

    facts: list[dict[str, Any]] = []
    used_legacy_keys: set[tuple[str, str, str, str, str, str]] = set()
    stations: list[str] = []
    for capture in captures:
        if not isinstance(capture, Mapping):
            raise ContractError("retained_member_capture_mapping_required")
        _verify_content_hash(capture, "retained_member_capture_sha_invalid")
        station_id = _text(
            capture.get("station_id"), "retained_member_station_missing"
        ).upper()
        stations.append(station_id)
        event_date = _text(
            capture.get("event_date"), "retained_member_event_date_missing"
        )
        run_init, _capture_run_dt = _utc(
            capture.get("run_init_utc"), "retained_member_run_init_invalid"
        )
        archive_query, _archive_dt = _utc(
            capture.get("archive_query_at_utc"),
            "retained_member_archive_query_invalid",
        )
        settlement_timezone = _text(
            capture.get("settlement_timezone"),
            "retained_member_settlement_timezone_missing",
        )
        if event_date != packet_event_date or run_init != packet_run_init:
            raise ContractError("retained_member_capture_packet_scope_mismatch")
        for field, expected in (
            ("ensemble_system_id", spec.ensemble_system_id),
            ("independence_family_id", spec.independence_family_id),
            ("run_cycle", spec.run_cycle),
            ("source_class", SOURCE_CLASS),
        ):
            if str(capture.get(field) or "") != expected:
                raise ContractError("retained_member_capture_scope_mismatch:" + field)
        if capture.get("native_run_init_known") is not True:
            raise ContractError("retained_member_native_run_init_unknown")
        if capture.get("native_publication_at_known") is not False:
            raise ContractError("retained_member_publication_state_invalid")
        if capture.get("offline_chronological_oos_eligible") is not True:
            raise ContractError("retained_member_legacy_oos_flag_invalid")
        if capture.get("training_only") is not True or capture.get("holdout_eligible") is not False:
            raise ContractError("retained_member_capture_training_role_invalid")
        if capture.get("calls_order_gateway") is not False or capture.get("live_submit_authorized") is not False:
            raise ContractError("retained_member_capture_authority_invalid")
        if int(capture.get("orders_submitted") or 0) != 0:
            raise ContractError("retained_member_capture_orders_invalid")
        receipts = capture.get("field_receipts")
        if not isinstance(receipts, list) or not receipts:
            raise ContractError("retained_member_field_receipts_required")
        receipt_set_sha = _sha(
            capture.get("field_receipt_set_sha256"),
            "retained_member_receipt_set_sha_invalid",
        )
        if content_sha256(receipts) != receipt_set_sha:
            raise ContractError("retained_member_receipt_set_sha_mismatch")
        if int(capture.get("field_receipt_count") or -1) != len(receipts):
            raise ContractError("retained_member_receipt_count_mismatch")
        members = capture.get("members")
        if not isinstance(members, list) or len(members) != spec.expected_member_count:
            raise ContractError("retained_member_exact_member_count_required")
        member_identity = [
            {
                "member_id": item.get("member_id"),
                "member_role": item.get("member_role"),
                "raw_tmax_c": item.get("raw_tmax_c"),
            }
            for item in members
            if isinstance(item, Mapping)
        ]
        if len(member_identity) != len(members):
            raise ContractError("retained_member_member_mapping_required")
        if content_sha256(member_identity) != _sha(
            capture.get("member_set_sha256"),
            "retained_member_member_set_sha_invalid",
        ):
            raise ContractError("retained_member_member_set_sha_mismatch")
        if not _closed_day(
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            as_of=as_of_dt,
        ):
            raise ContractError("retained_member_event_day_not_closed")
        for member in members:
            assert isinstance(member, Mapping)
            member_id = _text(member.get("member_id"), "retained_member_id_missing")
            key = _legacy_key(
                spec.ensemble_system_id,
                station_id,
                event_date,
                spec.lead_bucket,
                spec.run_cycle,
                member_id,
            )
            if key in used_legacy_keys:
                raise ContractError("retained_member_legacy_scope_duplicate")
            source_row = legacy_rows.get(key)
            if source_row is None:
                raise ContractError("retained_member_legacy_row_missing")
            source_member_sha, source_archive_query = _validate_legacy_row(
                source_row,
                spec=spec,
                capture=capture,
                member=member,
                receipt_set_sha=receipt_set_sha,
            )
            used_legacy_keys.add(key)
            identity = {
                "station_id": station_id,
                "ensemble_system_id": spec.ensemble_system_id,
                "model_family": "global_ensemble",
                "provider_id": spec.provider_id,
                "source_class": CANONICAL_SOURCE_CLASS,
                "event_date": event_date,
                "run_init_utc": run_init,
                "lead_bucket": spec.lead_bucket,
                "run_cycle": spec.run_cycle,
                "member_id": member_id,
                "member_role": _text(
                    member.get("member_role"), "retained_member_role_missing"
                ),
                "extraction_version": spec.extraction_version,
                "settlement_timezone": settlement_timezone,
                "forecast_value": float(member.get("raw_tmax_c")),
                "raw_receipt_set_sha256": receipt_set_sha,
                "source_member_fact_sha": source_member_sha,
                "archive_query_at_known": True,
                # The content-addressed source member row owns this clock.
                # The packet archive query is separately validated above but
                # must not overwrite a different retained-row query time.
                "archive_query_at_utc": source_archive_query,
                # Legacy OOS substituted run-init for arrival.  Preserve the
                # member value, but do not preserve that temporal claim.
                "oos_role": CANONICAL_OOS_ROLE,
                "closed_day": True,
                "strict_asof": False,
            }
            facts.append({**identity, "content_sha256": content_sha256(identity)})
    if tuple(sorted(stations)) != canonical_station_ids():
        raise ContractError("retained_member_packet_station_set_invalid")
    if len(used_legacy_keys) != 45 * spec.expected_member_count:
        raise ContractError("retained_member_packet_member_coverage_invalid")
    contract_identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": as_of,
        "builder_id": _text(builder_id, "retained_member_builder_id_missing"),
        "model_facts": [],
        "ensemble_member_facts": facts,
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [],
        "residual_facts": [],
        "activation_facts": [],
    }
    contract = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    receipt_identity = {
        "schema": RECEIPT_SCHEMA,
        "adapter_schema": ADAPTER_SCHEMA,
        "source_packet_schema": schema,
        "source_packet_content_sha256": packet_sha,
        "ensemble_system_id": spec.ensemble_system_id,
        "event_date": packet_event_date,
        "lead_bucket": spec.lead_bucket,
        "run_cycle": spec.run_cycle,
        "canonical_member_fact_count": len(facts),
        "canonical_native_arrival_fact_count": 0,
        "canonical_oos_role": CANONICAL_OOS_ROLE,
        "strict_asof_member_count": 0,
        "native_arrival_indexed_count": 0,
        "blockers": ["LINEAGE_MISMATCH"],
        "blocker_detail": [
            "NATIVE_PUBLICATION_TIMESTAMP_UNVERIFIED",
            "NATIVE_RECEIVED_TIMESTAMP_UNVERIFIED",
            "LEGACY_RUN_INIT_WAS_NOT_ARRIVAL_EVIDENCE",
        ],
        "contract_content_sha256": contract["content_sha256"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "formal_live_authority": False,
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    return contract, receipt


def legacy_rows_for_packet(
    conn: Any,
    packet: Mapping[str, Any],
) -> dict[tuple[str, str, str, str, str, str], dict[str, Any]]:
    """Read the exact retained source rows required by one source packet."""

    schema = str(packet.get("schema") or "")
    spec = SOURCE_SPECS.get(schema)
    if spec is None:
        raise ContractError("retained_member_packet_schema_unsupported")
    event_date = _text(
        packet.get("event_date"), "retained_member_packet_event_date_missing"
    )
    columns = (*LEGACY_IDENTITY_FIELDS, "content_sha256")
    rows = conn.execute(
        "SELECT " + ",".join(columns) + " "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND target_local_date=? "
        "AND archive_lead_bucket=? AND run_cycle=?",
        (
            spec.ensemble_system_id,
            event_date,
            spec.lead_bucket,
            spec.run_cycle,
        ),
    ).fetchall()
    result: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        mapping = dict(zip(columns, row))
        key = _legacy_key(
            str(mapping["ensemble_system_id"]),
            str(mapping["station_id"]),
            str(mapping["target_local_date"]),
            str(mapping["archive_lead_bucket"]),
            str(mapping["run_cycle"]),
            str(mapping["member_id"]),
        )
        if key in result:
            raise ContractError("retained_member_legacy_scope_duplicate")
        result[key] = mapping
    return result


__all__ = [
    "ADAPTER_SCHEMA",
    "CANONICAL_OOS_ROLE",
    "LEGACY_IDENTITY_FIELDS",
    "RECEIPT_SCHEMA",
    "SOURCE_SPECS",
    "adapt_retained_packet_to_canonical_candidates",
    "legacy_rows_for_packet",
]
