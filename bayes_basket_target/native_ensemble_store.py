"""Append-only ingress for Open-Meteo native-ensemble candidates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .open_meteo_ensemble import ENSEMBLE_SYSTEMS, SCHEMA
from .ssot import native_ensemble_foundation_schema_status


def _insert_content_addressed(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    row: Mapping[str, Any],
) -> int:
    identifier = str(row[primary_key])
    existing = conn.execute(
        f"SELECT content_sha256 FROM {table} WHERE {primary_key}=?",
        (identifier,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError(
                f"native_ensemble_{table}_content_conflict"
            )
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


def ingest_forward_packet(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    status = native_ensemble_foundation_schema_status(conn)
    if status["ok"] is not True:
        raise ContractError(
            "native_ensemble_foundation_schema_required"
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
        or authority.get("formal_p0_eligible") is not False
        or not isinstance(systems, list)
        or not systems
    ):
        raise ContractError("native_ensemble_packet_authority_invalid")
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
            system_identity = {
                "ensemble_system_id": system_id,
                "canonical_name": config["canonical_name"],
                "independence_family_id": config[
                    "independence_family_id"
                ],
                "provider_id": config["provider_id"],
                # This row identifies the upstream forecast system.  The
                # Open-Meteo transport authority is recorded on every member
                # run and bucket below, where formal eligibility is decided.
                "source_class": "NATIVE_ENSEMBLE_PROVIDER",
                "aggregator_or_republisher": 0,
                "expected_member_count": config[
                    "expected_member_count"
                ],
                "member_identity_contract_version": (
                    "open_meteo_control_plus_perturbed_v1"
                ),
                "publication_contract_version": (
                    "native_timestamps_required_for_formal_v1"
                ),
                "valid_from_utc": "2026-07-31T00:00:00Z",
                "valid_until_utc": "9999-12-31T23:59:59Z",
            }
            system_row = {
                **system_identity,
                "content_sha256": content_sha256(
                    system_identity
                ),
            }
            counts["systems"] += _insert_content_addressed(
                conn,
                table="calibration_native_ensemble_system",
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
                "FROM calibration_native_ensemble_member_bucket "
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
                    "run_init_utc": None,
                    "member_id": str(member["member_id"]),
                    "member_role": str(member["member_role"]),
                    "published_at_utc": None,
                    "received_at_utc": captured_at,
                    "lead_bucket": lead_bucket,
                    "run_cycle": (
                        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
                    ),
                    "raw_tmax_c": float(member["raw_tmax_c"]),
                    "settlement_definition": (
                        "official_daily_tmax_c"
                    ),
                    "extraction_version": (
                        "open_meteo_daily_tmax_member_v1"
                    ),
                    "provider_id": config["provider_id"],
                    "transport_provider_id": "OPEN_METEO",
                    "native_timestamps_complete": 0,
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
                    "member_run_id": "native-member-" + member_sha[:32],
                    **member_identity,
                    "content_sha256": member_sha,
                }
                counts["member_runs"] += _insert_content_addressed(
                    conn,
                    table=(
                        "calibration_native_ensemble_member_run"
                    ),
                    primary_key="member_run_id",
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
                "member_bucket_id": (
                    "native-bucket-" + bucket_sha[:32]
                ),
                **bucket_identity,
                "content_sha256": bucket_sha,
            }
            counts["member_buckets"] += _insert_content_addressed(
                conn,
                table=(
                    "calibration_native_ensemble_member_bucket"
                ),
                primary_key="member_bucket_id",
                row=bucket_row,
            )
    except Exception:
        conn.execute(
            "ROLLBACK TO native_ensemble_forward_ingress"
        )
        conn.execute(
            "RELEASE native_ensemble_forward_ingress"
        )
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


__all__ = ["ingest_forward_packet"]
