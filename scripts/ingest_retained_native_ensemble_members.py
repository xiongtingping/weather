#!/usr/bin/env python3
"""Preview/apply retained GEFS/GEPS canonical member candidates.

This is a one-time, production-no-live migration owner.  It reads exact
content-addressed source packets and legacy rows, then writes only through the
canonical ingress.  It never fetches model data, creates native-arrival facts,
promotes strict OOS, starts services, or calls the order gateway.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import CanonicalContract  # noqa: E402
from bayes_basket_target.canonical_ingress import (  # noqa: E402
    _fact_rows,
    ingest_canonical_contract,
)
from bayes_basket_target.canonical_ssot_schema import (  # noqa: E402
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.retained_native_ensemble_canonical_adapter import (  # noqa: E402
    adapt_retained_packet_to_canonical_candidates,
    legacy_rows_for_packet,
)


SCHEMA = "retained_native_ensemble_canonical_ingress_manifest_v1"
CONFIRMATION = "ingest-retained-native-ensemble-members-production-no-live"
DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_HISTORY = ROOT / "output/audits/native_ensemble_history"
DEFAULT_OUTPUT = (
    ROOT
    / "output/audits/retained_native_ensemble_canonical_ingress_20260804.json"
)
DEFAULT_AS_OF = "2026-08-04T00:00:00Z"
BUILDER_ID = "retained-native-ensemble-candidate-adapter-v1"


def _days(start: date, end: date) -> list[date]:
    values: list[date] = []
    current = start
    while current <= end:
        values.append(current)
        current += timedelta(days=1)
    return values


def default_packet_paths(history: Path = DEFAULT_HISTORY) -> tuple[Path, ...]:
    """Return the explicit retained 44 GEFS + 28 GEPS packet inventory."""

    paths: list[Path] = []
    for value in _days(date(2026, 6, 18), date(2026, 6, 30)):
        paths.append(
            history / f"NOAA_GEFS_45station_{value:%Y%m%d}_official_d2.json"
        )
    for value in _days(date(2026, 7, 1), date(2026, 7, 29)):
        paths.append(history / f"GEFS_45station_{value:%Y%m%d}_official_d2.json")
    paths.extend(
        (
            history / "NOAA_GEFS_45station_20260730_official_18z_d2.json",
            history / "NOAA_GEFS_45station_20260731_closed_candidate_full_20260801.json",
        )
    )
    for value in _days(date(2026, 7, 3), date(2026, 7, 30)):
        paths.append(
            history
            / f"ECCC_GEPS_45station_{value:%Y%m%d}_official_00z_d2.json"
        )
    return tuple(paths)


def _load_packet(path: Path) -> Mapping[str, Any]:
    candidate = path.expanduser().resolve(strict=True)
    if candidate.is_symlink():
        raise ContractError("retained_member_source_symlink_forbidden")
    value = load_bounded_json(candidate)
    if not isinstance(value, Mapping):
        raise ContractError("retained_member_source_mapping_required")
    return value


def _canonical_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(fact_type): int(count)
        for fact_type, count in conn.execute(
            "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact "
            "GROUP BY fact_type ORDER BY fact_type"
        )
    }


def _read_only_replay_check(
    conn: sqlite3.Connection,
    contract: CanonicalContract,
) -> tuple[int, int]:
    insertable = 0
    replayed = 0
    for fact_id, fact_type, scope, fact_json, lineage_json in _fact_rows(contract):
        existing = conn.execute(
            "SELECT fact_type,scope_key,content_sha256,fact_json,lineage_json "
            "FROM canonical_ssot_fact WHERE fact_id=?",
            (fact_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (
                fact_type,
                scope,
                fact_id.split(":", 1)[1],
                fact_json,
                lineage_json,
            ):
                raise ContractError("retained_member_canonical_content_conflict")
            replayed += 1
            continue
        scope_existing = conn.execute(
            "SELECT fact_id FROM canonical_ssot_fact "
            "WHERE fact_type=? AND scope_key=?",
            (fact_type, scope),
        ).fetchone()
        if scope_existing is not None:
            raise ContractError("retained_member_canonical_scope_conflict")
        insertable += 1
    return insertable, replayed


def ingest_retained_packets(
    database: Path,
    *,
    packet_paths: Sequence[Path],
    as_of_utc: str,
    apply: bool,
    confirmation: str | None = None,
    writer_quiescence_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    database = database.expanduser().resolve(strict=True)
    if database.is_symlink():
        raise ContractError("retained_member_database_symlink_forbidden")
    if not packet_paths:
        raise ContractError("retained_member_source_inventory_empty")
    if apply and confirmation != CONFIRMATION:
        raise ContractError("retained_member_apply_confirmation_invalid")
    if apply and writer_quiescence_receipt is None:
        raise ContractError("retained_member_writer_quiescence_required")
    if not apply and (confirmation is not None or writer_quiescence_receipt is not None):
        raise ContractError("retained_member_preview_apply_argument_forbidden")

    resolved_paths = tuple(
        path.expanduser().resolve(strict=True) for path in packet_paths
    )
    if len({str(path) for path in resolved_paths}) != len(resolved_paths):
        raise ContractError("retained_member_source_inventory_duplicate")
    packet_inventory: list[dict[str, str]] = []
    mode = "rw" if apply else "ro"
    uri = f"{database.as_uri()}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True, timeout=60.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        if not apply:
            conn.execute("PRAGMA query_only=ON")
        schema_status = inspect_canonical_ssot_schema(conn)
        if schema_status["state"] not in {"V8", "V9", "V10"} or schema_status["errors"]:
            raise ContractError("retained_member_canonical_ssot_v7_required")
        counts_before = _canonical_counts(conn)
        validated_quiescence: Mapping[str, Any] | None = None
        if apply:
            try:
                validated_quiescence = validate_writer_quiescence_receipt(
                    writer_quiescence_receipt,
                    db_path=database,
                    require_current_database_identity=True,
                )
            except ValueError as exc:
                raise ContractError(
                    "retained_member_writer_quiescence_invalid"
                ) from exc
            conn.execute("BEGIN EXCLUSIVE")
            try:
                validated_quiescence = validate_writer_quiescence_receipt(
                    writer_quiescence_receipt,
                    db_path=database,
                    require_current_database_identity=True,
                )
            except ValueError as exc:
                conn.rollback()
                raise ContractError(
                    "retained_member_writer_quiescence_changed_before_lock"
                ) from exc

        adapter_receipts: list[dict[str, Any]] = []
        ingress_receipts: list[dict[str, Any]] = []
        insertable_total = 0
        replayed_total = 0
        try:
            for source_path in resolved_paths:
                packet = _load_packet(source_path)
                packet_inventory.append(
                    {
                        "path": str(source_path),
                        "schema": str(packet.get("schema") or ""),
                        "event_date": str(packet.get("event_date") or ""),
                        "content_sha256": str(
                            packet.get("content_sha256") or ""
                        ),
                    }
                )
                legacy_rows = legacy_rows_for_packet(conn, packet)
                contract_mapping, adapter_receipt = (
                    adapt_retained_packet_to_canonical_candidates(
                        packet,
                        legacy_rows=legacy_rows,
                        as_of_utc=as_of_utc,
                        builder_id=BUILDER_ID,
                    )
                )
                contract = CanonicalContract.from_mapping(contract_mapping)
                if apply:
                    ingress = ingest_canonical_contract(
                        conn,
                        contract,
                        commit=True,
                        finalize_transaction=False,
                        ingested_at_utc=as_of_utc,
                    )
                    insertable_total += int(ingress["inserted_count"])
                    replayed_total += int(ingress["replayed_count"])
                    ingress_receipts.append(ingress)
                else:
                    insertable, replayed = _read_only_replay_check(conn, contract)
                    insertable_total += insertable
                    replayed_total += replayed
                adapter_receipts.append(adapter_receipt)
            if apply:
                conn.commit()
        except Exception:
            if apply and conn.in_transaction:
                conn.rollback()
            raise
        counts_after = _canonical_counts(conn)
    finally:
        conn.close()

    by_system: dict[str, int] = {}
    for receipt in adapter_receipts:
        system_id = str(receipt["ensemble_system_id"])
        by_system[system_id] = by_system.get(system_id, 0) + int(
            receipt["canonical_member_fact_count"]
        )
    expected_after = dict(counts_before)
    expected_after["ensemble_member"] = int(
        expected_after.get("ensemble_member", 0)
    ) + (insertable_total if apply else 0)
    if apply and counts_after != expected_after:
        raise ContractError("retained_member_post_ingress_count_mismatch")
    identity = {
        "schema": SCHEMA,
        "state": "COMMITTED" if apply else "PREVIEW_VALIDATED_NO_WRITE",
        "database": str(database),
        "as_of_utc": as_of_utc,
        "builder_id": BUILDER_ID,
        "packet_count": len(resolved_paths),
        "packet_inventory_sha256": content_sha256(packet_inventory),
        "packet_inventory": packet_inventory,
        "canonical_member_fact_count_by_system": dict(sorted(by_system.items())),
        "canonical_member_fact_count": sum(by_system.values()),
        "canonical_native_arrival_fact_count": 0,
        "strict_asof_member_count": 0,
        "insertable_or_inserted_count": insertable_total,
        "replayed_count": replayed_total,
        "fact_type_counts_before": counts_before,
        "fact_type_counts_after": counts_after,
        "adapter_receipt_set_sha256": content_sha256(adapter_receipts),
        "ingress_receipt_set_sha256": (
            content_sha256(ingress_receipts) if apply else None
        ),
        "writer_quiescence_receipt_sha256": (
            str(validated_quiescence["receipt_sha256"])
            if validated_quiescence is not None
            else None
        ),
        "blockers": ["LINEAGE_MISMATCH"],
        "blocker_detail": [
            "NATIVE_PUBLICATION_TIMESTAMP_UNVERIFIED",
            "NATIVE_RECEIVED_TIMESTAMP_UNVERIFIED",
            "LEGACY_RUN_INIT_WAS_NOT_ARRIVAL_EVIDENCE",
        ],
        "writes_database": bool(apply and insertable_total),
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "starts_services": False,
        "changes_live_pause": False,
        "formal_live_authority": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--packet", type=Path, action="append")
    parser.add_argument("--as-of-utc", default=DEFAULT_AS_OF)
    parser.add_argument("--receipt-output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--apply-production-no-live", action="store_true")
    parser.add_argument("--confirm")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    apply = bool(args.apply_production_no_live)
    if apply and args.writer_quiescence_receipt is None:
        raise SystemExit("--apply-production-no-live requires --writer-quiescence-receipt")
    quiescence = (
        load_bounded_json(args.writer_quiescence_receipt)
        if args.writer_quiescence_receipt is not None
        else None
    )
    packet_paths = tuple(args.packet or default_packet_paths())
    receipt = ingest_retained_packets(
        args.database,
        packet_paths=packet_paths,
        as_of_utc=args.as_of_utc,
        apply=apply,
        confirmation=args.confirm,
        writer_quiescence_receipt=quiescence,
    )
    output = args.receipt_output.expanduser().resolve()
    write_bounded_json(output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
