#!/usr/bin/env python3
"""Build and optionally ingest canonical AWC market bucket bridges."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.market_bucket_bridge import (  # noqa: E402
    CONTRACT_SCHEMA,
    CanonicalMarketBucketBridgeContract,
    build_market_bucket_bridge_contract,
)
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


CONFIRMATION = "commit-market-bucket-bridge-production-no-live-v11"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--registry-export", type=Path, required=True)
    station_group = parser.add_mutually_exclusive_group(required=True)
    station_group.add_argument("--station-id", action="append")
    station_group.add_argument("--all-45-stations", action="store_true")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--commit-production-no-live-v11", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    commit = bool(args.commit_production_no_live_v11)
    if commit and args.writer_quiescence_receipt is None:
        parser.error("commit requires --writer-quiescence-receipt")
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not commit and (
        args.writer_quiescence_receipt is not None or args.confirm is not None
    ):
        parser.error("quiescence and confirmation are commit-only")

    supplied = args.database.expanduser()
    if supplied.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = supplied.resolve(strict=True)
    registry_export = load_bounded_json(
        args.registry_export.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    if commit:
        quiescence = load_bounded_json(
            args.writer_quiescence_receipt.expanduser().resolve(strict=True),
            limit=MAX_JSON_INPUT_BYTES,
        )
        validate_writer_quiescence_receipt(
            quiescence,
            db_path=database,
            require_current_database_identity=True,
        )

    station_ids = (
        coordinate_registry_manifest()["station_ids"]
        if args.all_45_stations
        else sorted({str(item).upper() for item in args.station_id})
    )
    ready_facts: list[dict[str, object]] = []
    blocked_scopes: list[dict[str, str]] = []
    normalized_as_of = ""
    builder_id = ""
    for station_id in station_ids:
        try:
            station_packet = build_market_bucket_bridge_contract(
                registry_export,
                station_id=station_id,
                event_date=args.event_date,
                as_of_utc=args.as_of_utc,
            )
        except ContractError as exc:
            reason = str(exc)
            blocked_scopes.append(
                {
                    "station_id": station_id,
                    "blocker_code": (
                        "MARKET_BUCKET_BRIDGE_MISSING"
                        if reason == "market_bucket_bridge_scope_missing"
                        else "MARKET_BUCKET_BRIDGE_INVALID"
                    ),
                    "blocker_detail": reason,
                }
            )
            continue
        normalized_as_of = str(station_packet["as_of_utc"])
        builder_id = str(station_packet["builder_id"])
        ready_facts.extend(station_packet["bridge_facts"])

    if not ready_facts:
        receipt = {
            "schema": "canonical_market_bucket_bridge_materialization_receipt_v1",
            "state": "BLOCKED",
            "database": str(database),
            "station_ids": station_ids,
            "event_date": str(args.event_date),
            "registry_snapshot_sha256": registry_export.get("snapshot_sha256"),
            "expected_scope_count": len(station_ids),
            "ready_scope_count": 0,
            "blocked_scope_count": len(blocked_scopes),
            "blocked_scopes": blocked_scopes,
            "coverage_terminal": len(blocked_scopes) == len(station_ids),
            "production_no_live_only": True,
            "writes_database": False,
            "p0_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        receipt = {**receipt, "content_sha256": content_sha256(receipt)}
        write_bounded_json(
            args.receipt_output.expanduser().resolve(),
            receipt,
            limit=MAX_JSON_INPUT_BYTES,
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    packet_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": normalized_as_of,
        "builder_id": builder_id,
        "bridge_facts": sorted(
            ready_facts, key=lambda item: (item["station_id"], item["event_date"])
        ),
    }
    packet = {
        **packet_identity,
        "content_sha256": content_sha256(packet_identity),
    }
    CanonicalMarketBucketBridgeContract.from_mapping(packet)
    with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        if not commit:
            conn.execute("PRAGMA query_only=ON")
        before = int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE fact_type='market_bucket_bridge'"
            ).fetchone()[0]
        )
        ingress = ingest_canonical_contract(
            conn,
            packet,
            commit=commit,
            ingested_at_utc=args.as_of_utc,
        )
        after = int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE fact_type='market_bucket_bridge'"
            ).fetchone()[0]
        )
    expected_delta = int(ingress["inserted_count"]) if commit else 0
    if after - before != expected_delta:
        raise SystemExit("market_bucket_bridge_count_mismatch")

    write_bounded_json(
        args.contract_output.expanduser().resolve(),
        packet,
        limit=MAX_JSON_INPUT_BYTES,
    )
    receipt = {
        "schema": "canonical_market_bucket_bridge_materialization_receipt_v1",
        "state": ingress["state"],
        "database": str(database),
        "station_ids": station_ids,
        "event_date": str(args.event_date),
        "registry_snapshot_sha256": registry_export["snapshot_sha256"],
        "contract_content_sha256": packet["content_sha256"],
        "bridge_content_sha256s": [
            item["content_sha256"] for item in packet["bridge_facts"]
        ],
        "expected_scope_count": len(station_ids),
        "ready_scope_count": len(ready_facts),
        "blocked_scope_count": len(blocked_scopes),
        "blocked_scopes": blocked_scopes,
        "coverage_terminal": len(ready_facts) + len(blocked_scopes) == len(station_ids),
        "bridge_count_before": before,
        "bridge_count_after": after,
        "ingress_receipt": ingress,
        "production_no_live_only": True,
        "p0_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {**receipt, "content_sha256": content_sha256(receipt)}
    write_bounded_json(
        args.receipt_output.expanduser().resolve(),
        receipt,
        limit=MAX_JSON_INPUT_BYTES,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
