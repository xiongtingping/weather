#!/usr/bin/env python3
"""Materialize bounded, no-live native-ensemble scenario-role evidence.

The command consumes one already captured native-ensemble inbox (either the
Open-Meteo republisher packet or the official-native forward packet) and an
authoritative station-day market registry snapshot.  It only writes a
content-addressed scenario-evidence JSON file.  P0, P0-M, P1, SQLite, the
order gateway, and execution state are intentionally outside this command.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_market_registry_snapshot,
)
from bayes_basket_target.native_ensemble_scenario_roles import (  # noqa: E402
    METHOD_VERSION,
    select_native_ensemble_scenario_roles,
)
from bayes_basket_target.p0_producer import (  # noqa: E402
    bucket_intervals_from_registry_rows,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    ResourceBoundError,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "native_ensemble_scenario_roles_inbox_v1"
STATION_CANDIDATE_SCHEMA = "native_ensemble_scenario_roles_station_v1"
INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
OPEN_METEO_PACKET_SCHEMA = "open_meteo_native_ensemble_forward_packet_v1"
OFFICIAL_PACKET_SCHEMA = "official_native_ensemble_forward_packet_v1"
MAX_STATIONS = 45
MAX_SYSTEMS_PER_STATION = 128
MAX_MEMBERS_PER_SYSTEM = 512
MAX_BUCKETS = 256
MAX_INPUT_PACKETS = 1024
MAX_TOTAL_MEMBER_ROWS = 45 * MAX_SYSTEMS_PER_STATION * MAX_MEMBERS_PER_SYSTEM


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"scenario_cli_{name}_missing")
    return result


def _station(value: Any) -> str:
    return _text("station_id", value).upper()


def _optional_station(value: Any) -> str:
    return str(value or "").strip().upper()


def _event_date(value: Any) -> str:
    result = _text("event_date", value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError("scenario_cli_event_date_invalid") from exc
    return result


def _verified_content_hash(
    raw: Mapping[str, Any],
    *,
    hash_key: str,
    error: str,
) -> str:
    supplied = str(raw.get(hash_key) or "")
    identity = {key: value for key, value in raw.items() if key != hash_key}
    if len(supplied) != 64 or supplied != content_sha256(identity):
        raise ContractError(error)
    return supplied


def _verified_inbox(raw: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(raw, Mapping):
        raise ContractError("scenario_cli_inbox_invalid")
    if raw.get("schema") != INBOX_SCHEMA:
        raise ContractError("scenario_cli_inbox_schema_invalid")
    packets = raw.get("packets")
    if (
        not isinstance(packets, list)
        or not packets
        or len(packets) > MAX_INPUT_PACKETS
    ):
        raise ContractError("scenario_cli_inbox_packets_invalid")
    inbox_hash = _verified_content_hash(
        raw,
        hash_key="content_sha256",
        error="scenario_cli_inbox_content_sha256_mismatch",
    )
    verified_packets: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ContractError("scenario_cli_packet_invalid")
        packet_schema = str(packet.get("schema") or "")
        if packet_schema == OPEN_METEO_PACKET_SCHEMA:
            packet_hash = _verified_content_hash(
                packet,
                hash_key="packet_content_sha256",
                error="scenario_cli_open_meteo_packet_hash_mismatch",
            )
        elif packet_schema == OFFICIAL_PACKET_SCHEMA:
            packet_hash = _verified_content_hash(
                packet,
                hash_key="snapshot_content_sha256",
                error="scenario_cli_official_packet_hash_mismatch",
            )
        else:
            raise ContractError("scenario_cli_packet_schema_invalid")
        station_id = _station(packet.get("station_id"))
        event_date = _event_date(packet.get("event_date"))
        timezone = _text(
            "settlement_timezone", packet.get("settlement_timezone")
        )
        systems = packet.get("systems")
        intervals = packet.get("bucket_intervals")
        if (
            not isinstance(systems, list)
            or not systems
            or len(systems) > MAX_SYSTEMS_PER_STATION
            or not isinstance(intervals, Mapping)
            or not intervals
            or len(intervals) > MAX_BUCKETS
        ):
            raise ContractError("scenario_cli_packet_shape_invalid")
        member_rows = 0
        for system in systems:
            if not isinstance(system, Mapping):
                raise ContractError("scenario_cli_system_invalid")
            members = system.get("members")
            if isinstance(members, list):
                if not members or len(members) > MAX_MEMBERS_PER_SYSTEM:
                    raise ContractError("scenario_cli_system_members_invalid")
                member_rows += len(members)
        if member_rows > MAX_SYSTEMS_PER_STATION * MAX_MEMBERS_PER_SYSTEM:
            raise ContractError("scenario_cli_station_member_bound_exceeded")
        verified = dict(packet)
        verified["station_id"] = station_id
        verified["event_date"] = event_date
        verified["settlement_timezone"] = timezone
        verified["_packet_content_sha256"] = packet_hash
        verified["_source_kind"] = (
            "open_meteo_aggregator"
            if packet_schema == OPEN_METEO_PACKET_SCHEMA
            else "official_native"
        )
        verified_packets.append(verified)
    result = dict(raw)
    result["packets"] = verified_packets
    return result, inbox_hash


def _verified_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("scenario_cli_registry_invalid")
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ContractError("scenario_cli_registry_rows_missing")
    # load_market_registry_snapshot already verifies the snapshot's own
    # content.  Keep this local shape check because tests and callers may pass
    # an in-memory snapshot directly to build_scenario_roles_artifact().
    if len(rows) > 45 * MAX_BUCKETS:
        raise ContractError("scenario_cli_registry_rows_bound_exceeded")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ContractError("scenario_cli_registry_row_invalid")
    return dict(raw)


def _registry_scope(
    registry: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any]:
    matching = [
        row
        for row in registry["rows"]
        if _optional_station(row.get("station_id")) == station_id
        and str(row.get("event_date") or "") == event_date
        and int(row.get("market_active") or 0) == 1
    ]
    if not matching:
        raise ContractError(
            "scenario_cli_registry_scope_missing:"
            f"{station_id}:{event_date}"
        )
    if any(int(row.get("source_eligible") or 0) != 1 for row in matching):
        raise ContractError(
            "scenario_cli_registry_scope_not_source_eligible:"
            f"{station_id}:{event_date}"
        )
    outcomes = [str(row.get("outcome") or "").strip() for row in matching]
    if not outcomes or any(not outcome for outcome in outcomes):
        raise ContractError("scenario_cli_registry_outcome_missing")
    if len(set(outcomes)) != len(outcomes):
        raise ContractError("scenario_cli_registry_outcome_duplicate")
    timezones = {str(row.get("settlement_timezone") or "").strip() for row in matching}
    if len(timezones) != 1 or "" in timezones:
        raise ContractError(
            "scenario_cli_registry_timezone_invalid:"
            f"{station_id}:{event_date}"
        )
    intervals_raw = bucket_intervals_from_registry_rows(matching)
    intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals_raw.items()
    }
    ordered = sorted(
        intervals.items(),
        key=lambda item: (
            float("-inf") if item[1][0] is None else float(item[1][0]),
            float("inf") if item[1][1] is None else float(item[1][1]),
            item[0],
        ),
    )
    ordinals = {bucket: index for index, (bucket, _bounds) in enumerate(ordered)}
    row_identity = [
        {
            "slug": str(row.get("slug") or ""),
            "outcome": str(row.get("outcome") or ""),
            "station_id": _optional_station(row.get("station_id")),
            "event_date": str(row.get("event_date") or ""),
            "settlement_timezone": str(row.get("settlement_timezone") or ""),
            "settlement_unit": str(row.get("settlement_unit") or ""),
            "source_lane": str(row.get("source_lane") or ""),
            "source_eligible": int(row.get("source_eligible") or 0),
            "rules_sha256": str(row.get("rules_sha256") or ""),
            "registry_json_sha256": content_sha256(
                str(row.get("registry_json") or "")
            ),
        }
        for row in sorted(matching, key=lambda item: str(item.get("outcome") or ""))
    ]
    authority_identity = {
        "schema": "native_ensemble_scenario_market_registry_contract_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": next(iter(timezones)),
        "bucket_intervals": intervals,
        "rows": row_identity,
    }
    return {
        "schema": authority_identity["schema"],
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": next(iter(timezones)),
        "bucket_intervals": intervals,
        "bucket_ordinals": ordinals,
        "bucket_contract_sha256": content_sha256(intervals),
        "authority_content_sha256": content_sha256(authority_identity),
        "registry_snapshot_sha256": str(
            registry.get("snapshot_sha256")
            or registry.get("content_sha256")
            or ""
        ),
        "row_count": len(matching),
    }


def _source_archive_lineage(
    packet: Mapping[str, Any],
    *,
    system_id: str,
) -> dict[str, Any]:
    archives = packet.get("source_archives")
    if not isinstance(archives, list):
        return {}
    matches = [
        row
        for row in archives
        if isinstance(row, Mapping)
        and str(row.get("ensemble_system_id") or "") == system_id
    ]
    if len(matches) != 1:
        return {}
    row = matches[0]
    return {
        "ensemble_system_id": system_id,
        "transport_provider_id": str(row.get("transport_provider_id") or ""),
        "run_init_utc": str(row.get("run_init_utc") or ""),
        "archive_content_sha256": str(row.get("archive_content_sha256") or ""),
        "station_capture_content_sha256": str(
            row.get("station_capture_content_sha256") or ""
        ),
        "source_bucket_contract_sha256": str(
            row.get("source_bucket_contract_sha256") or ""
        ),
    }


def _prepare_products(
    packet: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    inbox_content_sha256: str,
) -> list[dict[str, Any]]:
    systems = packet["systems"]
    source_kind = str(packet["_source_kind"])
    packet_hash = str(packet["_packet_content_sha256"])
    packet_intervals = packet.get("bucket_intervals")
    registry_intervals = contract["bucket_intervals"]
    source_contract_sha = str(packet.get("bucket_contract_sha256") or "")
    registry_contract_sha = str(contract["bucket_contract_sha256"])
    products: list[dict[str, Any]] = []
    for raw_system in systems:
        product = dict(raw_system)
        system_id = _text(
            "ensemble_system_id",
            product.get("ensemble_system_id")
            or product.get("system_id")
            or product.get("product_id"),
        )
        family_id = _text(
            "independence_family_id",
            product.get("independence_family_id")
            or product.get("family_id"),
        )
        members = product.get("members")
        if members is not None and (
            not isinstance(members, list)
            or not members
            or len(members) > MAX_MEMBERS_PER_SYSTEM
        ):
            raise ContractError("scenario_cli_product_members_invalid")
        # The registry is authoritative for this station-day.  A member list
        # is therefore always re-bucketed by the pure selector.  Source raw
        # probabilities are discarded when members are present so an old or
        # differently binned transport curve cannot silently win.
        if isinstance(members, list):
            for key in (
                "raw_bucket_probabilities",
                "raw_probabilities",
                "bucket_probabilities",
                "raw_bucket_counts",
            ):
                product.pop(key, None)
        elif product.get("raw_bucket_probabilities") is not None:
            if (
                not isinstance(packet_intervals, Mapping)
                or source_contract_sha != registry_contract_sha
            ):
                raise ContractError(
                    "scenario_cli_product_members_required_for_registry_rebucket"
                )
        else:
            raise ContractError("scenario_cli_product_members_missing")
        product["ensemble_system_id"] = system_id
        product["independence_family_id"] = family_id
        product["source_class"] = (
            "aggregator_republisher"
            if source_kind == "open_meteo_aggregator"
            else "official_native_archive"
        )
        product["aggregator_or_republisher"] = (
            source_kind == "open_meteo_aggregator"
        )
        product["content_sha256"] = packet_hash
        product["lineage"] = {
            "schema": "native_ensemble_scenario_source_lineage_v1",
            "source_kind": source_kind,
            "inbox_content_sha256": inbox_content_sha256,
            "packet_content_sha256": packet_hash,
            "station_id": str(packet["station_id"]),
            "event_date": str(packet["event_date"]),
            "source_bucket_contract_sha256": source_contract_sha,
            "registry_bucket_contract_sha256": registry_contract_sha,
            "official_archive": _source_archive_lineage(
                packet, system_id=system_id
            ),
        }
        # Avoid passing an untrusted packet contract into the selector when
        # members are present; explicit registry intervals are the only
        # interval source for the selection.
        products.append(product)
    if len(products) > MAX_SYSTEMS_PER_STATION:
        raise ContractError("scenario_cli_products_bound_exceeded")
    return products


def _packet_index(
    packets: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for packet in packets:
        key = (str(packet["station_id"]), str(packet["event_date"]))
        if key in result:
            raise ContractError(
                "scenario_cli_duplicate_packet_scope:" + ":".join(key)
            )
        result[key] = packet
    return result


def _target_scopes(
    packets: Sequence[Mapping[str, Any]],
    registry: Mapping[str, Any],
    *,
    station_ids: Sequence[str] | None,
    event_date: str | None,
    max_stations: int,
) -> list[tuple[str, str]]:
    if max_stations < 1 or max_stations > MAX_STATIONS:
        raise ContractError("scenario_cli_max_stations_invalid")
    packet_index = _packet_index(packets)
    requested_stations = sorted({_station(item) for item in station_ids or []})
    if len(requested_stations) > max_stations:
        raise ContractError("scenario_cli_station_scope_exceeds_bound")
    packet_dates = {str(packet["event_date"]) for packet in packets}
    registry_dates = {
        str(row.get("event_date") or "")
        for row in registry["rows"]
        if str(row.get("event_date") or "") in packet_dates
        and int(row.get("market_active") or 0) == 1
    }
    target_date = event_date
    if target_date is not None:
        target_date = _event_date(target_date)
        if target_date not in packet_dates:
            raise ContractError("scenario_cli_event_date_packet_missing")
    elif len(registry_dates) != 1:
        raise ContractError("scenario_cli_event_date_required")
    else:
        target_date = next(iter(registry_dates))
    if requested_stations:
        stations = requested_stations
    else:
        stations = sorted(
            {
                str(packet["station_id"])
                for packet in packets
                if str(packet["event_date"]) == target_date
            }
        )
        stations = [
            station
            for station in stations
            if any(
                _optional_station(row.get("station_id")) == station
                and str(row.get("event_date") or "") == target_date
                and int(row.get("market_active") or 0) == 1
                for row in registry["rows"]
            )
        ]
    if not stations:
        raise ContractError("scenario_cli_station_scope_empty")
    if len(stations) > max_stations:
        raise ContractError("scenario_cli_station_scope_exceeds_bound")
    scopes: list[tuple[str, str]] = []
    for station in stations:
        key = (station, target_date)
        if key not in packet_index:
            raise ContractError(
                "scenario_cli_packet_scope_missing:" + ":".join(key)
            )
        # Resolve the registry contract now so missing/ineligible rows fail
        # before any output is written.
        _registry_scope(registry, station_id=station, event_date=target_date)
        scopes.append(key)
    return scopes


def build_scenario_roles_artifact(
    *,
    inbox: Mapping[str, Any],
    registry: Mapping[str, Any],
    station_ids: Sequence[str] | None = None,
    event_date: str | None = None,
    max_stations: int = MAX_STATIONS,
) -> dict[str, Any]:
    """Build one bounded role-evidence artifact from verified JSON mappings."""

    verified_inbox, inbox_hash = _verified_inbox(inbox)
    verified_registry = _verified_registry(registry)
    packets = verified_inbox["packets"]
    scopes = _target_scopes(
        packets,
        verified_registry,
        station_ids=station_ids,
        event_date=event_date,
        max_stations=max_stations,
    )
    packet_index = _packet_index(packets)
    total_members = 0
    candidates: list[dict[str, Any]] = []
    source_kinds: set[str] = set()
    for station_id, target_date in scopes:
        packet = packet_index[(station_id, target_date)]
        contract = _registry_scope(
            verified_registry,
            station_id=station_id,
            event_date=target_date,
        )
        products = _prepare_products(
            packet,
            contract=contract,
            inbox_content_sha256=inbox_hash,
        )
        total_members += sum(
            len(product.get("members") or [])
            for product in products
            if isinstance(product.get("members"), list)
        )
        if total_members > MAX_TOTAL_MEMBER_ROWS:
            raise ContractError("scenario_cli_batch_member_bound_exceeded")
        source_kind = str(packet["_source_kind"])
        source_kinds.add(source_kind)
        selection = select_native_ensemble_scenario_roles(
            products=products,
            bucket_ordinals=contract["bucket_ordinals"],
            bucket_intervals=contract["bucket_intervals"],
            station_id=station_id,
            event_date=target_date,
            settlement_timezone=contract["settlement_timezone"],
            p1_center_bucket=packet.get("p1_center_bucket"),
            p0m_center_bucket=packet.get("p0m_center_bucket"),
            p0_m_center_bucket=packet.get("p0_m_center_bucket"),
            p0_center_bucket=packet.get("p0_center_bucket"),
        )
        # The pure selector intentionally exposes compatibility aliases
        # (m_bucket/roles/selected, observations/observation_candidates and
        # observations_by_side).  Persisting all aliases once per station
        # multiplied a legitimate 40-station artifact beyond the 16 MiB
        # output ceiling.  Keep one canonical copy of roles and observations,
        # plus the selector hash and compact provenance needed to audit the
        # exact selection, instead of weakening the global resource bound.
        selection_lineage = {
            "schema": str(selection["schema"]),
            "method_version": str(selection["method_version"]),
            "content_sha256": str(selection["content_sha256"]),
            "bucket_contract_sha256": str(
                selection["bucket_contract_sha256"]
            ),
            "center_buckets": selection["center_buckets"],
            "source_product_ids": selection["source_product_ids"],
            "source_family_ids": selection["source_family_ids"],
            "source_product_lineage": selection["source_product_lineage"],
            "consensus_candidate_count": int(
                selection["consensus_candidate_count"]
            ),
        }
        candidate_identity = {
            "schema": STATION_CANDIDATE_SCHEMA,
            "method_version": METHOD_VERSION,
            "station_id": station_id,
            "event_date": target_date,
            "settlement_timezone": contract["settlement_timezone"],
            "source_kind": source_kind,
            "source_packet_content_sha256": str(
                packet["_packet_content_sha256"]
            ),
            "market_registry": contract,
            "role_selection_lineage": selection_lineage,
            "buckets": selection["buckets"],
            "roles": selection["roles"],
            "observation_candidates": selection["observation_candidates"],
            "blockers": selection["blockers"],
            "raw_member_probability_is_not_p0_position": True,
            "position_authority": "P0/P0-M/P1_AND_EXECUTION_POLICY",
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }
        candidates.append({
            **candidate_identity,
            "content_sha256": content_sha256(candidate_identity),
        })
    candidates.sort(key=lambda row: (row["station_id"], row["event_date"]))
    identity = {
        "schema": SCHEMA,
        "method_version": METHOD_VERSION,
        "event_date": str(candidates[0]["event_date"]),
        "station_scope": [row["station_id"] for row in candidates],
        "station_count": len(candidates),
        "source_kinds": sorted(source_kinds),
        "input_inbox_schema": str(verified_inbox["schema"]),
        "input_inbox_content_sha256": inbox_hash,
        "market_registry_snapshot_sha256": str(
            verified_registry.get("snapshot_sha256")
            or verified_registry.get("content_sha256")
            or ""
        ),
        "candidates": candidates,
        "resource_bounds": {
            "max_stations": max_stations,
            "max_total_member_rows": MAX_TOTAL_MEMBER_ROWS,
            "max_json_input_bytes": MAX_JSON_INPUT_BYTES,
            "max_json_output_bytes": MAX_JSON_OUTPUT_BYTES,
            "actual_station_count": len(candidates),
            "actual_total_member_rows": total_members,
        },
        "raw_member_probability_is_not_p0_position": True,
        "position_authority": "P0/P0-M/P1_AND_EXECUTION_POLICY",
        "writes_p0": False,
        "writes_p0m": False,
        "writes_p1": False,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def materialize_scenario_roles(
    *,
    inbox_path: Path,
    registry_path: Path,
    output_path: Path,
    station_ids: Sequence[str] | None = None,
    event_date: str | None = None,
    max_stations: int = MAX_STATIONS,
) -> tuple[dict[str, Any], bool]:
    """Read bounded inputs, build evidence, and atomically write one output."""

    inbox = load_bounded_json(inbox_path.expanduser().resolve(strict=True))
    registry = load_market_registry_snapshot(
        registry_path.expanduser().resolve(strict=True)
    )
    artifact = build_scenario_roles_artifact(
        inbox=inbox,
        registry=registry,
        station_ids=station_ids,
        event_date=event_date,
        max_stations=max_stations,
    )
    wrote = write_bounded_json_if_changed(output_path, artifact)
    return artifact, wrote


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--market-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--event-date")
    parser.add_argument(
        "--max-stations",
        type=int,
        choices=tuple(range(1, MAX_STATIONS + 1)),
        default=MAX_STATIONS,
    )
    args = parser.parse_args(argv)
    try:
        artifact, wrote = materialize_scenario_roles(
            inbox_path=args.inbox,
            registry_path=args.market_registry,
            output_path=args.output,
            station_ids=args.station_id,
            event_date=args.event_date,
            max_stations=args.max_stations,
        )
    except (ContractError, ResourceBoundError, OSError, ValueError) as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "status": "BLOCKED",
            "blocker": str(exc),
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({
        "schema": SCHEMA,
        "status": "PASS",
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
        "station_count": artifact["station_count"],
        "event_date": artifact["event_date"],
        "buckets_missing_roles_allowed": True,
        "raw_member_probability_is_not_p0_position": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
