#!/usr/bin/env python3
"""Build sequential hard-fact P1 packets from formal canonical observations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import load_started_registry_rows  # noqa: E402
from bayes_basket_target.p1_compiler import (  # noqa: E402
    canonical_materialization_packet,
    compile_station_p1_packets,
    materialization_packet,
    reported_hard_lower_in_unit as _reported_hard_lower_in_unit,
    select_p1_replay_base,
)
from bayes_basket_target.production_io import load_materialized_probability_envelopes  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_p1_packet_build_v1"
CANONICAL_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"


def build_packets(
    *,
    db_path: Path,
    canonical_evidence_path: Path,
    event_date: str,
    as_of_utc: str,
    output_dir: Path,
    freshness_seconds: int = 3600,
    station_ids: set[str] | None = None,
) -> dict[str, Any]:
    evidence = load_bounded_json(canonical_evidence_path)
    if not isinstance(evidence, Mapping) or evidence.get("schema") != CANONICAL_SCHEMA:
        raise ContractError("p1_canonical_evidence_invalid")
    canonical_receipt_sha256 = str(
        evidence.get("receipt_sha256") or ""
    ).strip().lower()
    canonical_identity = {
        key: value
        for key, value in evidence.items()
        if key not in {"receipt_sha256", "output"}
    }
    if (
        len(canonical_receipt_sha256) != 64
        or canonical_receipt_sha256 != content_sha256(canonical_identity)
    ):
        raise ContractError("p1_canonical_evidence_receipt_mismatch")
    observations = evidence.get("observations")
    if not isinstance(observations, list):
        raise ContractError("p1_canonical_observations_missing")
    observed_stations = sorted({
        str(item.get("station_id") or "")
        for item in observations
        if isinstance(item, Mapping)
        and item.get("event_date_local") == event_date
        and item.get("evidence_role") == "FORMAL"
        and item.get("qc_status") == "ACCEPTED"
    } - {""})
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    db = resolve_target_database(db_path)
    requested_stations = {
        str(station).strip().upper()
        for station in (station_ids or set())
        if str(station).strip()
    }
    packet_paths: list[str] = []
    packet_evidence: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    materialized_stations: set[str] = set()
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        registry_scope_rows = load_started_registry_rows(
            conn,
            as_of_utc=as_of_utc,
            event_date=event_date,
        )
        if requested_stations:
            registry_scope_rows = [
                row for row in registry_scope_rows
                if str(row.get("station_id") or "").strip().upper()
                in requested_stations
            ]
        stations = sorted({
            str(row.get("station_id") or "").strip()
            for row in registry_scope_rows
        } - {""})
        if not stations:
            blockers.append({
                "station_id": "",
                "reason": "p1_started_registry_scope_empty",
            })
        missing_requested_stations = sorted(
            requested_stations - set(stations)
        )
        blockers.extend({
            "station_id": station,
            "reason": "p1_requested_station_not_in_started_registry",
        } for station in missing_requested_stations)
        for station in stations:
            formal_station_observations = [
                item for item in observations
                if isinstance(item, Mapping)
                and item.get("station_id") == station
                and item.get("event_date_local") == event_date
                and item.get("evidence_role") == "FORMAL"
                and item.get("qc_status") == "ACCEPTED"
            ]
            if not formal_station_observations:
                blockers.append({
                    "station_id": station,
                    "reason": "p1_formal_observation_missing",
                })
                continue
            try:
                envelopes = load_materialized_probability_envelopes(
                    conn,
                    station_id=station,
                    event_date=event_date,
                    evaluation_at_utc=as_of_utc,
                )
                existing_p1 = any(
                    envelope.stage == "P1" for envelope in envelopes
                )
                (
                    prediction_root,
                    predecessor,
                    consumed,
                    rebasing,
                ) = select_p1_replay_base(
                    envelopes, as_of_utc=as_of_utc
                )
                registry_rows = load_started_registry_rows(
                    conn,
                    as_of_utc=as_of_utc,
                    station_id=station,
                    event_date=event_date,
                )
                packets = compile_station_p1_packets(
                    predecessor=predecessor,
                    prediction_root=prediction_root,
                    observations=formal_station_observations,
                    registry_rows=registry_rows,
                    compilation_at_utc=as_of_utc,
                    freshness_seconds=freshness_seconds,
                    consumed_observation_ids=consumed,
                )
                if packets or existing_p1:
                    materialized_stations.add(station)
                else:
                    blockers.append({
                        "station_id": station,
                        "reason": "p1_no_materializable_packet",
                    })
                for ordinal, packet in enumerate(packets):
                    packet["p0_rebase"] = rebasing
                    packet["p0_root_checkpoint_id"] = (
                        prediction_root.checkpoint_id
                        if prediction_root.stage == "P0"
                        else str(
                            prediction_root.lineage.get(
                                "p0_root_checkpoint_id"
                            )
                            or ""
                        )
                    )
                    path = destination / f"{station}_{event_date}_p1_{ordinal:03d}.json"
                    write_bounded_json(
                        path,
                        canonical_materialization_packet(packet),
                    )
                    expected = ProbabilityEnvelope.from_mapping(
                        packet["expected_envelope"]
                    )
                    packet_paths.append(str(path))
                    packet_evidence.append({
                        "path": str(path),
                        "station_id": station,
                        "observation_id": str(packet.get("observation_id") or ""),
                        "hard_bound_id": str(packet.get("hard_bound_id") or ""),
                        "p0_rebase": bool(packet.get("p0_rebase")),
                        "p0_root_checkpoint_id": str(packet.get("p0_root_checkpoint_id") or ""),
                        "expected_envelope_sha256": expected.content_sha256,
                    })
            except (ContractError, ValueError, TypeError, sqlite3.Error) as exc:
                blockers.append({"station_id": station, "reason": str(exc)})
    result = {
        "schema": SCHEMA,
        "event_date": event_date,
        "as_of_utc": as_of_utc,
        "canonical_evidence": {
            "path": str(canonical_evidence_path.expanduser().resolve()),
            "receipt_sha256": canonical_receipt_sha256,
        },
        "station_scope": {
            "mode": "allowlist" if requested_stations else "all_started_registry",
            "requested_station_ids": sorted(requested_stations),
        },
        "station_count": len(stations),
        "registry_station_count": len(stations),
        "registry_stations": stations,
        "observed_station_count": len(observed_stations),
        "observed_stations": observed_stations,
        "materialized_station_count": len(materialized_stations),
        "materialized_stations": sorted(materialized_stations),
        "coverage_complete": (
            bool(stations)
            and len(materialized_stations) == len(stations)
            and not blockers
        ),
        "packet_count": len(packet_paths),
        "blocked_count": len(blockers),
        "packet_paths": packet_paths,
        "packet_evidence": packet_evidence,
        "blockers": blockers,
        "boundary": {
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    write_bounded_json(destination / "manifest.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--freshness-seconds", type=int, default=3600)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_packets(
        db_path=args.db,
        canonical_evidence_path=args.canonical_evidence,
        event_date=args.event_date,
        as_of_utc=args.as_of_utc,
        output_dir=args.output_dir,
        freshness_seconds=args.freshness_seconds,
        station_ids=set(args.station_id),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
