"""Append-only ingress for Open-Meteo native-ensemble candidates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .open_meteo_ensemble import (
    ENSEMBLE_SYSTEMS,
    FORWARD_OOS_TEMPORAL_SCOPE,
    OBSERVATIONAL_EVIDENCE_ROLE,
    SCHEMA,
)
from .ssot import republisher_ensemble_schema_status


_LEGACY_REPUBLISHER_PRODUCT_CONTRACTS = frozenset(
    {
        # Rows written by the pre-split republisher migration are immutable.
        # Their negative native/formal flags and explicit republisher role are
        # still valid; only the publication-contract label was refined later.
        "native_timestamps_required_for_formal_v1",
    }
)


def _legacy_republisher_product_compatible(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """Accept an immutable pre-contract-version product row.

    The target database is append-only, so an old product row cannot be
    rewritten merely because the republisher contract received a more precise
    name.  We only reuse it when every semantic field is identical apart from
    that label and the row still carries the explicit non-native boundary.
    Any other mismatch remains a hard content conflict.
    """

    if (
        str(existing.get("publication_contract_version") or "")
        not in _LEGACY_REPUBLISHER_PRODUCT_CONTRACTS
        or str(existing.get("source_role") or "")
        != "aggregator_republisher"
        or str(existing.get("transport_provider_id") or "")
        != "OPEN_METEO"
        or int(existing.get("native_arrival_index_eligible") or 0) != 0
        or int(existing.get("formal_p0_eligible") or 0) != 0
    ):
        return False
    for key, value in candidate.items():
        if key in {"content_sha256", "publication_contract_version"}:
            continue
        if str(existing.get(key)) != str(value):
            return False
    return True


def _insert_content_addressed(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    row: Mapping[str, Any],
) -> int:
    identifier = str(row[primary_key])
    existing_cursor = conn.execute(
        f"SELECT * FROM {table} WHERE {primary_key}=?",
        (identifier,),
    )
    existing_raw = existing_cursor.fetchone()
    if existing_raw is not None:
        columns = tuple(column[0] for column in existing_cursor.description or ())
        existing = dict(zip(columns, existing_raw))
        if str(existing.get("content_sha256")) != str(row["content_sha256"]):
            if table != "calibration_republisher_ensemble_product" or not _legacy_republisher_product_compatible(
                existing,
                row,
            ):
                raise ContractError(
                    f"native_ensemble_{table}_content_conflict"
                )
            # This is an immutable, semantically identical legacy product
            # row.  Treat it as an idempotent replay and preserve its original
            # content hash rather than attempting an UPDATE.
            return 0
        return 0
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )
    return 1


def _lead_bucket(
    *,
    event_date: str,
    settlement_timezone: str,
    captured_at_utc: str,
) -> str:
    captured = datetime.fromisoformat(
        captured_at_utc.replace("Z", "+00:00")
    ).astimezone(ZoneInfo(settlement_timezone))
    delta = (
        datetime.fromisoformat(event_date).date()
        - captured.date()
    ).days
    return f"d{delta}" if delta >= 0 else f"d_minus_{abs(delta)}"


def republisher_product_identity(
    system_id: str,
) -> dict[str, Any]:
    """Return product metadata without claiming native transport authority."""

    config = ENSEMBLE_SYSTEMS.get(system_id)
    if config is None:
        raise ContractError("republisher_ensemble_system_unregistered")
    return {
        "ensemble_system_id": system_id,
        "canonical_name": str(config["canonical_name"]),
        "independence_family_id": str(
            config["independence_family_id"]
        ),
        "upstream_provider_id": str(config["provider_id"]),
        "transport_provider_id": "OPEN_METEO",
        "source_role": "aggregator_republisher",
        "expected_member_count": int(config["expected_member_count"]),
        "member_identity_contract_version": (
            "open_meteo_control_plus_perturbed_v1"
        ),
        "publication_contract_version": (
            "aggregator_receipt_without_native_timestamps_v1"
        ),
        "native_arrival_index_eligible": 0,
        "formal_p0_eligible": 0,
        "valid_from_utc": "2026-07-31T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }


def _require_forward_observational_scope(
    packet: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> None:
    """Keep a forward Open-Meteo receipt out of historical OOS authority.

    Open-Meteo is useful when a native object is unavailable, but the forward
    capture itself is only an arrival/interface observation.  A separate,
    append-only historical contract tied to closed AWC truth is required
    before any republisher data can support a no-live P0 fallback.
    """

    if (
        packet.get("evidence_role") != OBSERVATIONAL_EVIDENCE_ROLE
        or packet.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or packet.get("strict_oos_sample_eligible") is not False
        or packet.get("can_substitute_for_formal_promotion") is not False
        or authority.get("evidence_role")
        != OBSERVATIONAL_EVIDENCE_ROLE
        or authority.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or authority.get("counts_as_strict_oos") is not False
        or authority.get("historical_oos_contract_bound") is not False
        or authority.get("can_substitute_for_formal_promotion")
        is not False
    ):
        raise ContractError(
            "native_ensemble_packet_observational_scope_invalid"
        )


def ingest_forward_packet(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    status = republisher_ensemble_schema_status(conn)
    if status["ok"] is not True:
        raise ContractError(
            "republisher_ensemble_schema_required"
        )
    if packet.get("schema") != SCHEMA:
        raise ContractError("native_ensemble_packet_schema_invalid")
    identity = {
        key: value
        for key, value in packet.items()
        if key != "packet_content_sha256"
    }
    if (
        str(packet.get("packet_content_sha256") or "")
        != content_sha256(identity)
    ):
        raise ContractError("native_ensemble_packet_sha_mismatch")
    authority = packet.get("source_authority")
    systems = packet.get("systems")
    if (
        not isinstance(authority, Mapping)
        or authority.get("source_role")
        != "aggregator_republisher"
        or authority.get("native_run_init_known") is not False
        or authority.get("native_publication_at_known") is not False
        or authority.get("native_arrival_index_eligible") is not False
        or authority.get("formal_p0_eligible") is not False
        or authority.get("production_no_live_calibration_candidate")
        is not True
        or not isinstance(systems, list)
        or not systems
    ):
        raise ContractError("native_ensemble_packet_authority_invalid")
    _require_forward_observational_scope(packet, authority)
    station_id = str(packet.get("station_id") or "").strip().upper()
    event_date = str(packet.get("event_date") or "")
    settlement_timezone = str(
        packet.get("settlement_timezone") or ""
    )
    captured_at = str(
        packet.get("aggregator_snapshot_at_utc") or ""
    )
    bucket_contract_sha = str(
        packet.get("bucket_contract_sha256") or ""
    )
    if (
        not station_id
        or not event_date
        or not settlement_timezone
        or not captured_at
        or len(bucket_contract_sha) != 64
    ):
        raise ContractError("native_ensemble_packet_scope_invalid")
    if conn.execute(
        "SELECT 1 FROM calibration_station_registry WHERE station_id=?",
        (station_id,),
    ).fetchone() is None:
        raise ContractError("native_ensemble_station_unregistered")
    lead_bucket = _lead_bucket(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        captured_at_utc=captured_at,
    )
    counts = {
        "systems": 0,
        "member_runs": 0,
        "member_buckets": 0,
    }
    conn.execute("SAVEPOINT native_ensemble_forward_ingress")
    try:
        for raw_system in systems:
            if not isinstance(raw_system, Mapping):
                raise ContractError(
                    "native_ensemble_system_packet_invalid"
                )
            system_id = str(
                raw_system.get("ensemble_system_id") or ""
            )
            config = ENSEMBLE_SYSTEMS.get(system_id)
            if config is None:
                raise ContractError(
                    "native_ensemble_system_unregistered"
                )
            if (
                str(raw_system.get("independence_family_id") or "")
                != str(config["independence_family_id"])
                or int(raw_system.get("member_count") or 0)
                != int(config["expected_member_count"])
            ):
                raise ContractError(
                    "native_ensemble_system_identity_mismatch"
                )
            # AIFS is currently observed only through Open-Meteo.  Require
            # the per-system negative authority on every new AIFS packet so
            # an envelope-wide republisher label cannot conceal a forged
            # native arrival / formal-P0 upgrade.
            if config.get("candidate_only"):
                candidate_authority = raw_system.get("candidate_authority")
                if (
                    not isinstance(candidate_authority, Mapping)
                    or candidate_authority.get("source_role")
                    != "aggregator_republisher"
                    or candidate_authority.get("candidate_only") is not True
                    or candidate_authority.get("formal_p0_eligible")
                    is not False
                    or candidate_authority.get(
                        "native_arrival_index_eligible"
                    ) is not False
                    or candidate_authority.get(
                        "official_native_arrival_status"
                    ) != "MISSING_FAIL_CLOSED"
                ):
                    raise ContractError(
                        "native_ensemble_aifs_candidate_authority_invalid"
                    )
            # Product identity is useful for model/family calibration, but
            # Open-Meteo cannot populate the native-only registry.  Its
            # transport authority is explicit in this dedicated candidate
            # lane and cannot satisfy a native arrival/P0 lookup.
            system_identity = republisher_product_identity(system_id)
            system_row = {
                **system_identity,
                "content_sha256": content_sha256(
                    system_identity
                ),
            }
            counts["systems"] += _insert_content_addressed(
                conn,
                table="calibration_republisher_ensemble_product",
                primary_key="ensemble_system_id",
                row=system_row,
            )
            members = raw_system.get("members")
            if not isinstance(members, list):
                raise ContractError(
                    "native_ensemble_members_missing"
                )
            bucket_intervals_json = json.dumps(
                packet["bucket_intervals"],
                sort_keys=True,
                separators=(",", ":"),
            )
            raw_probabilities_json = json.dumps(
                raw_system["raw_bucket_probabilities"],
                sort_keys=True,
                separators=(",", ":"),
            )
            # Open-Meteo may republish an unchanged upstream member set with
            # a new aggregator receipt time/response object.  The member-set
            # hash is the semantic identity: an unchanged set is a zero-write
            # replay, not a second pseudo run.
            existing_bucket = conn.execute(
                "SELECT bucket_intervals_json,"
                "raw_bucket_probabilities_json,source_role,authority_scope "
                "FROM calibration_republisher_ensemble_member_bucket "
                "WHERE ensemble_system_id=? AND station_id=? "
                "AND target_local_date=? AND member_set_sha256=? "
                "AND bucket_contract_sha256=?",
                (
                    system_id,
                    station_id,
                    event_date,
                    str(raw_system["member_set_sha256"]),
                    bucket_contract_sha,
                ),
            ).fetchone()
            if existing_bucket is not None:
                if tuple(str(value) for value in existing_bucket) != (
                    bucket_intervals_json,
                    raw_probabilities_json,
                    "aggregator_republisher",
                    "PRODUCTION_NO_LIVE_CANDIDATE",
                ):
                    raise ContractError(
                        "native_ensemble_member_set_semantic_conflict"
                    )
                continue
            for member in members:
                if not isinstance(member, Mapping):
                    raise ContractError(
                        "native_ensemble_member_invalid"
                    )
                member_identity = {
                    "ensemble_system_id": system_id,
                    "station_id": station_id,
                    "target_local_date": event_date,
                    "member_id": str(member["member_id"]),
                    "member_role": str(member["member_role"]),
                    "received_at_utc": captured_at,
                    "lead_bucket": lead_bucket,
                    "raw_tmax_c": float(member["raw_tmax_c"]),
                    "settlement_definition": (
                        "official_daily_tmax_c"
                    ),
                    "extraction_version": (
                        "open_meteo_daily_tmax_member_v1"
                    ),
                    "upstream_provider_id": config["provider_id"],
                    "transport_provider_id": "OPEN_METEO",
                    "aggregator_snapshot_at_utc": captured_at,
                    "source_url": str(packet["request_url"]),
                    "raw_object_ref": (
                        "open_meteo_response_sha256:"
                        + str(packet["response_sha256"])
                    ),
                    "source_object_sha256": str(
                        packet["response_sha256"]
                    ),
                    "qc_status": "ACCEPTED",
                    "authority_scope": (
                        "PRODUCTION_NO_LIVE_CANDIDATE"
                    ),
                }
                member_sha = content_sha256(member_identity)
                member_row = {
                    "republisher_member_run_id": (
                        "republisher-member-" + member_sha[:32]
                    ),
                    **member_identity,
                    "content_sha256": member_sha,
                }
                counts["member_runs"] += _insert_content_addressed(
                    conn,
                    table=(
                        "calibration_republisher_ensemble_member_run"
                    ),
                    primary_key="republisher_member_run_id",
                    row=member_row,
                )
            bucket_identity = {
                "ensemble_system_id": system_id,
                "station_id": station_id,
                "target_local_date": event_date,
                "aggregator_snapshot_at_utc": captured_at,
                "member_set_sha256": str(
                    raw_system["member_set_sha256"]
                ),
                "member_count": int(raw_system["member_count"]),
                "bucket_contract_sha256": bucket_contract_sha,
                "bucket_intervals_json": bucket_intervals_json,
                "raw_bucket_probabilities_json": raw_probabilities_json,
                "source_role": "aggregator_republisher",
                "authority_scope": (
                    "PRODUCTION_NO_LIVE_CANDIDATE"
                ),
            }
            bucket_sha = content_sha256(bucket_identity)
            bucket_row = {
                    "republisher_member_bucket_id": (
                    "republisher-bucket-" + bucket_sha[:32]
                    ),
                **bucket_identity,
                "content_sha256": bucket_sha,
            }
            counts["member_buckets"] += _insert_content_addressed(
                conn,
                table=(
                    "calibration_republisher_ensemble_member_bucket"
                ),
                primary_key="republisher_member_bucket_id",
                row=bucket_row,
            )
    except Exception:
        # SQLite may abort the transaction on I/O failure and remove the
        # savepoint before control reaches this handler.  Cleanup must never
        # mask the original error with ``no such savepoint``; the resident
        # worker needs the original ``disk I/O error`` to reopen its handle.
        try:
            conn.execute(
                "ROLLBACK TO native_ensemble_forward_ingress"
            )
        except sqlite3.Error:
            pass
        try:
            conn.execute(
                "RELEASE native_ensemble_forward_ingress"
            )
        except sqlite3.Error:
            pass
        raise
    conn.execute("RELEASE native_ensemble_forward_ingress")
    return {
        "schema": "native_ensemble_forward_ingress_v1",
        "state": "COMMITTED",
        "station_id": station_id,
        "event_date": event_date,
        "snapshot_content_sha256": packet[
            "snapshot_content_sha256"
        ],
        "counts": counts,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


__all__ = ["ingest_forward_packet", "republisher_product_identity"]
