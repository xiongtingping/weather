"""Training-only Open-Meteo archived-ensemble member capture and ingress.

Past-days responses are archived forecasts, but they do not expose native
run-init/publication clocks.  They may seed calibration before a future
strict holdout; they are never holdout observations or direct P0 authority.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .native_ensemble_store import (
    _insert_content_addressed,
    republisher_product_identity,
)
from .open_meteo_ensemble import (
    API_URL,
    ENSEMBLE_SYSTEMS,
    VARIABLE,
    _bounded_get_json,
    _extract_target_members,
    _member_bucket_probabilities,
)
from .p0_producer import bucket_intervals_from_registry_rows
from .ssot import republisher_ensemble_schema_status


SCHEMA = "open_meteo_native_ensemble_historical_archive_v1"
MAX_HISTORY_DAYS = 31
DOCUMENTATION_URL = "https://open-meteo.com/en/docs/ensemble-api"
ARCHIVED_DAILY_EXTRACTION_VERSION = (
    "open_meteo_archived_daily_tmax_member_v1"
)
PREVIOUS_DAY1_EXTRACTION_VERSION = (
    "open_meteo_previous_day1_hourly_tmax_member_v1"
)
ALLOWED_EXTRACTION_VERSIONS = {
    ARCHIVED_DAILY_EXTRACTION_VERSION,
    PREVIOUS_DAY1_EXTRACTION_VERSION,
}


def capture_historical_archive(
    *,
    station_id: str,
    event_dates: Sequence[str],
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    ensemble_system_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    days = sorted(set(str(value or "").strip() for value in event_dates))
    if (
        not station
        or not days
        or len(days) > MAX_HISTORY_DAYS
        or not settlement_timezone
        or not math.isfinite(float(latitude))
        or not math.isfinite(float(longitude))
    ):
        raise ContractError("native_ensemble_history_scope_invalid")
    parsed_days = [date.fromisoformat(value) for value in days]
    if (parsed_days[-1] - parsed_days[0]).days >= MAX_HISTORY_DAYS:
        raise ContractError("native_ensemble_history_range_too_wide")
    query_clock = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    )
    local_query_day = query_clock.astimezone(
        ZoneInfo(settlement_timezone)
    ).date()
    if any(day >= local_query_day for day in parsed_days):
        raise ContractError(
            "native_ensemble_history_must_precede_query_day"
        )
    intervals = bucket_intervals_from_registry_rows(registry_rows)
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    bucket_contract_sha = content_sha256(frozen_intervals)
    selected_system_ids = tuple(
        sorted(
            {
                str(value or "").strip()
                for value in (
                    ENSEMBLE_SYSTEMS
                    if ensemble_system_ids is None
                    else ensemble_system_ids
                )
                if str(value or "").strip()
            }
        )
    )
    if not selected_system_ids or any(
        value not in ENSEMBLE_SYSTEMS for value in selected_system_ids
    ):
        raise ContractError("native_ensemble_history_system_selection_invalid")
    query = urlencode({
        "latitude": float(latitude),
        "longitude": float(longitude),
        "daily": VARIABLE,
        "models": ",".join(
            str(ENSEMBLE_SYSTEMS[system_id]["api_model"])
            for system_id in selected_system_ids
        ),
        "timezone": settlement_timezone,
        "start_date": days[0],
        "end_date": days[-1],
    })
    url = f"{API_URL}?{query}"
    payload, response_sha = _bounded_get_json(
        url,
        timeout_seconds=timeout_seconds,
    )
    captures: list[dict[str, Any]] = []
    for event_date in days:
        systems: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []
        for system_id in selected_system_ids:
            config = ENSEMBLE_SYSTEMS[system_id]
            try:
                members = _extract_target_members(
                    payload,
                    event_date=event_date,
                    expected_count=int(config["expected_member_count"]),
                    ensemble_system_id=system_id,
                    api_model=str(config["api_model"]),
                )
            except ContractError as exc:
                unavailable.append({
                    "ensemble_system_id": system_id,
                    "reason": str(exc),
                })
                continue
            member_identity = [
                {
                    "member_id": row["member_id"],
                    "member_role": row["member_role"],
                    "raw_tmax_c": row["raw_tmax_c"],
                }
                for row in members
            ]
            systems.append({
                "ensemble_system_id": system_id,
                "independence_family_id": config[
                    "independence_family_id"
                ],
                "member_count": len(members),
                "members": members,
                "member_set_sha256": content_sha256(member_identity),
                "raw_bucket_probabilities":
                    _member_bucket_probabilities(members, intervals),
            })
        capture_identity = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "archive_valid_day_bucket": "d0",
            "systems": systems,
            "unavailable_products": unavailable,
        }
        captures.append({
            **capture_identity,
            "capture_content_sha256": content_sha256(
                capture_identity
            ),
        })
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_dates": days,
        "event_date_count": len(days),
        "successful_event_date_count": sum(
            bool(row["systems"]) for row in captures
        ),
        "unavailable_event_date_count": sum(
            not bool(row["systems"]) for row in captures
        ),
        "settlement_timezone": settlement_timezone,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "archive_query_at_utc": archive_query_at_utc,
        "request_url": url,
        "response_sha256": response_sha,
        "bucket_contract_sha256": bucket_contract_sha,
        "bucket_intervals": frozen_intervals,
        "captures": captures,
        "selected_system_ids": list(selected_system_ids),
        "archive_contract": {
            "documentation_url": DOCUMENTATION_URL,
            "provider_describes_past_days_as_archived_forecasts": True,
            "native_run_init_known": False,
            "native_publication_at_known": False,
            "native_arrival_index_eligible": False,
            "training_only": True,
            "holdout_eligible": False,
            "formal_p0_eligible": False,
        },
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def ingest_historical_archive(
    conn: sqlite3.Connection,
    archive: Mapping[str, Any],
) -> dict[str, Any]:
    if republisher_ensemble_schema_status(conn)["ok"] is not True:
        raise ContractError("republisher_ensemble_schema_required")
    if archive.get("schema") != SCHEMA:
        raise ContractError("native_ensemble_history_schema_invalid")
    identity = {
        key: value
        for key, value in archive.items()
        if key != "content_sha256"
    }
    if str(archive.get("content_sha256") or "") != content_sha256(
        identity
    ):
        raise ContractError("native_ensemble_history_sha_mismatch")
    contract = archive.get("archive_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("training_only") is not True
        or contract.get("holdout_eligible") is not False
        or contract.get("native_run_init_known") is not False
        or contract.get("native_publication_at_known") is not False
    ):
        raise ContractError(
            "native_ensemble_history_authority_invalid"
        )
    station = str(archive.get("station_id") or "").strip().upper()
    extraction_version = str(
        archive.get("extraction_version")
        or ARCHIVED_DAILY_EXTRACTION_VERSION
    )
    if extraction_version not in ALLOWED_EXTRACTION_VERSIONS:
        raise ContractError(
            "native_ensemble_history_extraction_version_invalid"
        )
    query_at = str(archive.get("archive_query_at_utc") or "")
    response_sha = str(archive.get("response_sha256") or "")
    bucket_sha = str(archive.get("bucket_contract_sha256") or "")
    if (
        not station
        or not query_at
        or len(response_sha) != 64
        or len(bucket_sha) != 64
    ):
        raise ContractError("native_ensemble_history_scope_invalid")
    if conn.execute(
        "SELECT 1 FROM calibration_station_registry WHERE station_id=?",
        (station,),
    ).fetchone() is None:
        raise ContractError("native_ensemble_history_station_unregistered")
    counts = {"systems": 0, "member_runs": 0, "member_buckets": 0}
    revision_observation_count = 0
    conn.execute("SAVEPOINT native_ensemble_history_ingress")
    try:
        for capture in archive.get("captures") or []:
            if not isinstance(capture, Mapping):
                raise ContractError(
                    "native_ensemble_history_capture_invalid"
                )
            capture_identity = {
                key: value
                for key, value in capture.items()
                if key != "capture_content_sha256"
            }
            if str(capture.get("capture_content_sha256") or "") != (
                content_sha256(capture_identity)
            ):
                raise ContractError(
                    "native_ensemble_history_capture_sha_mismatch"
                )
            event_date = str(capture.get("event_date") or "")
            for raw_system in capture.get("systems") or []:
                system_id = str(
                    raw_system.get("ensemble_system_id") or ""
                )
                config = ENSEMBLE_SYSTEMS.get(system_id)
                if (
                    config is None
                    or str(
                        raw_system.get("independence_family_id")
                        or ""
                    )
                    != str(config["independence_family_id"])
                    or int(raw_system.get("member_count") or 0)
                    != int(config["expected_member_count"])
                ):
                    raise ContractError(
                        "native_ensemble_history_system_invalid"
                    )
                # Historical Open-Meteo is a training candidate only.  Keep
                # the product catalog outside the native registry so a
                # shared upstream model name cannot become arrival evidence.
                system_identity = republisher_product_identity(system_id)
                system_row = {
                    **system_identity,
                    "content_sha256": content_sha256(system_identity),
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
                        "native_ensemble_history_members_missing"
                    )
                incoming_members = {
                    str(member.get("member_id") or ""): member
                    for member in members
                    if isinstance(member, Mapping)
                }
                if (
                    len(incoming_members)
                    != int(config["expected_member_count"])
                    or "" in incoming_members
                ):
                    raise ContractError(
                        "native_ensemble_history_member_set_invalid"
                    )
                bucket_intervals_json = json.dumps(
                    archive["bucket_intervals"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                raw_probabilities_json = json.dumps(
                    raw_system["raw_bucket_probabilities"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing_members = conn.execute(
                    "SELECT member_id,member_role,raw_tmax_c,provider_id,"
                    "transport_provider_id,authority_scope,holdout_eligible "
                    "FROM calibration_republisher_ensemble_"
                    "historical_member_run "
                    "WHERE ensemble_system_id=? AND station_id=? "
                    "AND target_local_date=? "
                    "AND archive_lead_bucket='d0' "
                    "AND extraction_version=?",
                    (
                        system_id,
                        station,
                        event_date,
                        extraction_version,
                    ),
                ).fetchall()
                if existing_members:
                    if len(existing_members) != int(
                        config["expected_member_count"]
                    ):
                        raise ContractError(
                            "native_ensemble_history_frozen_member_set_"
                            "incomplete"
                        )
                    revised = False
                    for existing_member in existing_members:
                        member = incoming_members.get(
                            str(existing_member[0])
                        )
                        if member is None:
                            revised = True
                            break
                        expected = (
                            str(member["member_role"]),
                            float(member["raw_tmax_c"]),
                            str(config["provider_id"]),
                            "OPEN_METEO",
                            "CALIBRATION_TRAIN_ONLY",
                            0,
                        )
                        if (
                            str(existing_member[1]) != expected[0]
                            or abs(
                                float(existing_member[2])
                                - expected[1]
                            ) > 1e-9
                            or tuple(existing_member[3:])
                            != expected[2:]
                        ):
                            revised = True
                            break
                    if revised:
                        # Archived forecasts can be revised by the
                        # republisher.  Freeze the first locally observed
                        # training snapshot and report later versions; never
                        # overwrite it or let a revision become pseudo-OOS.
                        revision_observation_count += 1
                        continue
                    existing_bucket = conn.execute(
                        "SELECT bucket_intervals_json,"
                        "raw_bucket_probabilities_json,source_role,"
                        "authority_scope,holdout_eligible "
                        "FROM calibration_republisher_ensemble_"
                        "historical_member_bucket "
                        "WHERE ensemble_system_id=? AND station_id=? "
                        "AND target_local_date=? "
                        "AND archive_lead_bucket='d0' "
                        "AND member_set_sha256=? "
                        "AND bucket_contract_sha256=?",
                        (
                            system_id,
                            station,
                            event_date,
                            str(raw_system["member_set_sha256"]),
                            bucket_sha,
                        ),
                    ).fetchone()
                    if existing_bucket is None or tuple(
                        existing_bucket
                    ) != (
                        bucket_intervals_json,
                        raw_probabilities_json,
                        "aggregator_republisher",
                        "CALIBRATION_TRAIN_ONLY",
                        0,
                    ):
                        raise ContractError(
                            "native_ensemble_history_frozen_bucket_"
                            "mismatch"
                        )
                    continue
                for member in members:
                    member_identity = {
                        "ensemble_system_id": system_id,
                        "station_id": station,
                        "target_local_date": event_date,
                        "archive_lead_bucket": "d0",
                        "run_cycle":
                            "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                        "member_id": str(member["member_id"]),
                        "member_role": str(member["member_role"]),
                        "raw_tmax_c": float(member["raw_tmax_c"]),
                        "settlement_definition":
                            "official_daily_tmax_c",
                        "extraction_version":
                            extraction_version,
                        "provider_id": config["provider_id"],
                        "transport_provider_id": "OPEN_METEO",
                        "archive_query_at_utc": query_at,
                        "source_url": str(archive["request_url"]),
                        "raw_object_ref":
                            "open_meteo_response_sha256:" + response_sha,
                        "source_object_sha256": response_sha,
                        "qc_status": "ACCEPTED",
                        "authority_scope": "CALIBRATION_TRAIN_ONLY",
                        "holdout_eligible": 0,
                    }
                    member_hash = content_sha256(member_identity)
                    member_row = {
                            "historical_member_run_id":
                                "native-history-member-"
                                + member_hash[:28],
                            **member_identity,
                            "content_sha256": member_hash,
                    }
                    existing_member = conn.execute(
                        "SELECT member_role,raw_tmax_c,provider_id,"
                        "transport_provider_id,authority_scope,"
                        "holdout_eligible "
                        "FROM calibration_republisher_ensemble_"
                        "historical_member_run "
                        "WHERE ensemble_system_id=? AND station_id=? "
                        "AND target_local_date=? "
                        "AND archive_lead_bucket='d0' "
                        "AND member_id=? AND extraction_version=?",
                        (
                            system_id,
                            station,
                            event_date,
                            member_identity["member_id"],
                            member_identity["extraction_version"],
                        ),
                    ).fetchone()
                    if existing_member is not None:
                        expected = (
                            member_identity["member_role"],
                            member_identity["raw_tmax_c"],
                            member_identity["provider_id"],
                            "OPEN_METEO",
                            "CALIBRATION_TRAIN_ONLY",
                            0,
                        )
                        if (
                            str(existing_member[0]) != str(expected[0])
                            or abs(
                                float(existing_member[1])
                                - float(expected[1])
                            ) > 1e-9
                            or tuple(existing_member[2:]) != expected[2:]
                        ):
                            raise ContractError(
                                "native_ensemble_history_member_"
                                "semantic_conflict"
                            )
                    else:
                        counts["member_runs"] += (
                            _insert_content_addressed(
                                conn,
                                table=(
                                    "calibration_republisher_ensemble_"
                                    "historical_member_run"
                                ),
                                primary_key=(
                                    "historical_member_run_id"
                                ),
                                row=member_row,
                            )
                        )
                bucket_identity = {
                    "ensemble_system_id": system_id,
                    "station_id": station,
                    "target_local_date": event_date,
                    "archive_lead_bucket": "d0",
                    "archive_query_at_utc": query_at,
                    "member_set_sha256": str(
                        raw_system["member_set_sha256"]
                    ),
                    "member_count": int(raw_system["member_count"]),
                    "bucket_contract_sha256": bucket_sha,
                    "bucket_intervals_json": bucket_intervals_json,
                    "raw_bucket_probabilities_json":
                        raw_probabilities_json,
                    "source_role": "aggregator_republisher",
                    "authority_scope": "CALIBRATION_TRAIN_ONLY",
                    "holdout_eligible": 0,
                }
                bucket_hash = content_sha256(bucket_identity)
                bucket_row = {
                        "historical_member_bucket_id":
                            "native-history-bucket-" + bucket_hash[:28],
                        **bucket_identity,
                        "content_sha256": bucket_hash,
                }
                existing_bucket = conn.execute(
                    "SELECT bucket_intervals_json,"
                    "raw_bucket_probabilities_json,source_role,"
                    "authority_scope,holdout_eligible "
                    "FROM calibration_republisher_ensemble_"
                    "historical_member_bucket "
                    "WHERE ensemble_system_id=? AND station_id=? "
                    "AND target_local_date=? "
                    "AND archive_lead_bucket='d0' "
                    "AND member_set_sha256=? "
                    "AND bucket_contract_sha256=?",
                    (
                        system_id,
                        station,
                        event_date,
                        bucket_identity["member_set_sha256"],
                        bucket_sha,
                    ),
                ).fetchone()
                expected_bucket = (
                    bucket_identity["bucket_intervals_json"],
                    bucket_identity["raw_bucket_probabilities_json"],
                    "aggregator_republisher",
                    "CALIBRATION_TRAIN_ONLY",
                    0,
                )
                if existing_bucket is not None:
                    if tuple(existing_bucket) != expected_bucket:
                        raise ContractError(
                            "native_ensemble_history_bucket_"
                            "semantic_conflict"
                        )
                else:
                    counts["member_buckets"] += (
                        _insert_content_addressed(
                            conn,
                            table=(
                                "calibration_republisher_ensemble_"
                                "historical_member_bucket"
                            ),
                            primary_key=(
                                "historical_member_bucket_id"
                            ),
                            row=bucket_row,
                        )
                    )
    except Exception:
        # Some SQLite resource failures roll back the whole transaction and
        # therefore remove the savepoint.  Preserve the original exception;
        # a cleanup failure must never replace the real ingress blocker.
        if conn.in_transaction:
            try:
                conn.execute(
                    "ROLLBACK TO native_ensemble_history_ingress"
                )
                conn.execute(
                    "RELEASE native_ensemble_history_ingress"
                )
            except sqlite3.Error:
                pass
        raise
    conn.execute("RELEASE native_ensemble_history_ingress")
    return {
        "schema": "native_ensemble_history_ingress_v1",
        "state": "COMMITTED",
        "station_id": station,
        "event_date_count": int(archive["event_date_count"]),
        "successful_event_date_count": int(
            archive["successful_event_date_count"]
        ),
        "counts": counts,
        "revision_observation_count": revision_observation_count,
        "first_observed_training_snapshot_frozen": True,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = [
    "ARCHIVED_DAILY_EXTRACTION_VERSION",
    "MAX_HISTORY_DAYS",
    "PREVIOUS_DAY1_EXTRACTION_VERSION",
    "SCHEMA",
    "capture_historical_archive",
    "ingest_historical_archive",
]
