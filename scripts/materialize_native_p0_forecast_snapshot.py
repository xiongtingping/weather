#!/usr/bin/env python3
"""Build and optionally append one canonical native-ensemble P0 snapshot.

This entry point never collects model data, refreshes a market registry,
publishes PR04, starts services, or authorizes orders.  It consumes retained
official-native packets and lets the canonical builder enforce the exact
activation, model mask, lead/cycle, decision clock, and market bridge scope.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import (  # noqa: E402
    ingest_canonical_contract,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.p0_forecast_snapshot import (  # noqa: E402
    build_native_p0_forward_component_receipts,
    build_p0_forecast_snapshot_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


CONFIRMATION = "commit-native-p0-forecast-snapshot-production-no-live"
RECEIPT_SCHEMA = "canonical_native_p0_forecast_snapshot_materialization_receipt_v1"


def _packet(path: Path) -> tuple[Path, Mapping[str, Any]]:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise ContractError("native_p0_snapshot_source_packet_path_invalid")
    value = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("native_p0_snapshot_source_packet_mapping_required")
    return resolved, value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        existing = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
        if existing != dict(value):
            raise ContractError("native_p0_snapshot_sealed_output_rewrite_forbidden")
        return
    write_bounded_json_if_changed(
        resolved,
        dict(value),
        limit=MAX_JSON_INPUT_BYTES,
    )


def _sealed(identity: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(identity)
    return {**value, "content_sha256": content_sha256(value)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--activation-root-content-sha256", required=True)
    parser.add_argument("--market-bucket-bridge-content-sha256", required=True)
    parser.add_argument("--source-packet", type=Path, action="append", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--commit-production-no-live", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    commit = bool(args.commit_production_no_live)
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not commit and args.confirm is not None:
        parser.error("--confirm is commit-only")

    supplied_database = args.database.expanduser()
    if supplied_database.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = supplied_database.resolve(strict=True)
    station_id = str(args.station_id).strip().upper()
    if not station_id:
        raise SystemExit("station_id_required")

    packet_paths: list[str] = []
    component_receipts: list[dict[str, Any]] = []
    try:
        for source in args.source_packet:
            path, packet = _packet(source)
            packet_paths.append(str(path))
            component_receipts.extend(
                build_native_p0_forward_component_receipts(
                    packet,
                    source_packet_ref=path,
                    station_id=station_id,
                )
            )
        if len(set(packet_paths)) != len(packet_paths):
            raise ContractError("native_p0_snapshot_source_packet_duplicate")

        with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM canonical_ssot_fact "
                    "WHERE fact_type='p0_forecast_snapshot'"
                ).fetchone()[0]
            )
            contract = build_p0_forecast_snapshot_contract(
                conn,
                activation_root_content_sha256=str(
                    args.activation_root_content_sha256
                ),
                market_bucket_bridge_content_sha256=str(
                    args.market_bucket_bridge_content_sha256
                ),
                component_forward_receipts=component_receipts,
                as_of_utc=str(args.as_of_utc),
            )
            ingress = ingest_canonical_contract(
                conn,
                contract,
                commit=commit,
                ingested_at_utc=str(args.as_of_utc),
            )
            after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM canonical_ssot_fact "
                    "WHERE fact_type='p0_forecast_snapshot'"
                ).fetchone()[0]
            )
    except ContractError as exc:
        receipt = _sealed(
            {
                "schema": RECEIPT_SCHEMA,
                "state": "BLOCKED_FAIL_CLOSED",
                "blocker": str(exc),
                "database": str(database),
                "station_id": station_id,
                "source_packet_paths": packet_paths,
                "component_forward_receipt_count": len(component_receipts),
                "writes_database": False,
                "publishes_pr04_pointer": False,
                "starts_services": False,
                "releases_live_pause": False,
                "calls_order_gateway": False,
                "orders_submitted": 0,
            }
        )
        _write(args.receipt_output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    expected_delta = int(ingress["inserted_count"]) if commit else 0
    if after - before != expected_delta:
        raise SystemExit("native_p0_snapshot_count_mismatch")
    _write(args.contract_output, contract)
    receipt = _sealed(
        {
            "schema": RECEIPT_SCHEMA,
            "state": str(ingress["state"]),
            "database": str(database),
            "station_id": station_id,
            "source_packet_paths": packet_paths,
            "component_forward_receipt_count": len(component_receipts),
            "contract_content_sha256": str(contract["content_sha256"]),
            "snapshot_content_sha256s": [
                str(item["content_sha256"])
                for item in contract["snapshot_facts"]
            ],
            "snapshot_count_before": before,
            "snapshot_count_after": after,
            "ingress_receipt": ingress,
            "writes_database": commit,
            "production_no_live_only": True,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "releases_live_pause": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    )
    _write(args.receipt_output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
