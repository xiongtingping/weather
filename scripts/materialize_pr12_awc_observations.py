#!/usr/bin/env python3
"""Materialize PR12 v3 AWC facts through the only canonical ingress.

This command consumes the immutable v3 contract emitted by
``build_pr12_awc_observation_contract.py``.  It deliberately has no path to
the retired ``basket_target_observation_facts`` projection and never changes
PR04's current pointer, services, orders, or live pause.
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

from bayes_basket_target.awc_observation_contract import (  # noqa: E402
    CanonicalAWCObservationContract,
)
from bayes_basket_target.canonical_ingress import (  # noqa: E402
    ingest_canonical_contract,
    stage_awc_observation_contract,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


CONFIRMATION = "materialize-pr12-awc-observations-production-no-live"


def _sealed_builder_receipt(
    raw: Mapping[str, Any], *, contract: CanonicalAWCObservationContract
) -> dict[str, Any]:
    value = dict(raw)
    supplied = str(value.pop("receipt_sha256", "")).lower()
    if len(supplied) != 64 or supplied != content_sha256(value):
        raise ContractError("pr12_builder_receipt_hash_mismatch")
    if value.get("contract_content_sha256") != contract.content_sha256:
        raise ContractError("pr12_builder_receipt_contract_mismatch")
    if value.get("publishes_pr04_pointer") is not False or value.get(
        "starts_services"
    ) is not False:
        raise ContractError("pr12_builder_receipt_release_boundary_invalid")
    return {**value, "receipt_sha256": supplied}


def _fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        fact_type: int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
                (fact_type,),
            ).fetchone()[0]
        )
        for fact_type in ("truth_observation_revision", "truth_observation_day_bounds")
    }


def _blocked_receipt(
    *,
    database: Path,
    contract: CanonicalAWCObservationContract,
    builder_receipt: Mapping[str, Any],
    failure_code: str,
    detail: str,
    fact_counts_before: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    identity = {
        "schema": "bayes_basket_target_pr12_awc_materialization_receipt_v2",
        "state": "BLOCKED",
        "materialization_state": "NO_WRITE",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "failure_code": failure_code,
        "detail": detail,
        "database": str(database),
        "contract_content_sha256": contract.content_sha256,
        "builder_receipt_sha256": builder_receipt["receipt_sha256"],
        "terminal_gap_count": len(builder_receipt.get("terminal_gaps") or ()),
        "canonical_fact_counts_before": dict(fact_counts_before or {}),
        "canonical_rows_inserted": 0,
        "writes_legacy_projection": False,
        "current_pointer_changed": False,
        "production_service_started": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_pause_released": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--builder-receipt", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ingested-at-utc", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if args.apply and args.writer_quiescence_receipt is None:
        parser.error("--apply requires --writer-quiescence-receipt")
    if not args.apply and args.writer_quiescence_receipt is not None:
        parser.error("--writer-quiescence-receipt is apply-only")

    database = args.database.expanduser()
    if database.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = database.resolve(strict=True)
    contract = CanonicalAWCObservationContract.from_mapping(
        load_bounded_json(args.contract.expanduser().resolve(strict=True))
    )
    builder_receipt = _sealed_builder_receipt(
        load_bounded_json(args.builder_receipt.expanduser().resolve(strict=True)),
        contract=contract,
    )
    writer_quiescence_receipt: dict[str, Any] | None = None
    if args.writer_quiescence_receipt is not None:
        writer_quiescence_receipt = dict(
            load_bounded_json(args.writer_quiescence_receipt.expanduser().resolve(strict=True))
        )
        try:
            validate_writer_quiescence_receipt(
                writer_quiescence_receipt,
                db_path=database,
                require_current_database_identity=True,
            )
        except ValueError as exc:
            raise ContractError("pr12_writer_quiescence_invalid") from exc
    if not contract.revision_facts or not contract.day_bounds_facts:
        receipt = _blocked_receipt(
            database=database,
            contract=contract,
            builder_receipt=builder_receipt,
            failure_code="NO_CLOSED_AWC_FACTS",
            detail="builder produced only terminal gaps; no canonical fact is eligible",
        )
        write_bounded_json(args.receipt.expanduser().resolve(), receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    before: dict[str, int] = {}
    after: dict[str, int] = {}
    try:
        with sqlite3.connect(database, timeout=60.0) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            before = _fact_counts(conn)
            if args.apply:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    ingress_receipt = stage_awc_observation_contract(
                        conn, contract, ingested_at_utc=args.ingested_at_utc
                    )
                    after = _fact_counts(conn)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            else:
                ingress_receipt = ingest_canonical_contract(conn, contract, commit=False)
                after = before
    except (ContractError, sqlite3.Error) as exc:
        receipt = _blocked_receipt(
            database=database,
            contract=contract,
            builder_receipt=builder_receipt,
            failure_code="CANONICAL_INGRESS_REJECTED",
            detail=f"{type(exc).__name__}:{exc}",
            fact_counts_before=before,
        )
        write_bounded_json(args.receipt.expanduser().resolve(), receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    inserted = sum(after.values()) - sum(before.values()) if args.apply else 0
    identity = {
        "schema": "bayes_basket_target_pr12_awc_materialization_receipt_v2",
        "state": "MATERIALIZED_UNPUBLISHED",
        "materialization_state": (
            "MATERIALIZED_UNPUBLISHED" if args.apply else "VALIDATED_NO_WRITE"
        ),
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "database": str(database),
        "contract_content_sha256": contract.content_sha256,
        "builder_receipt_sha256": builder_receipt["receipt_sha256"],
        "terminal_gap_count": len(builder_receipt.get("terminal_gaps") or ()),
        "canonical_ingress_receipt": ingress_receipt,
        "canonical_fact_counts_before": before,
        "canonical_fact_counts_after": after,
        "canonical_rows_inserted": inserted,
        "writes_legacy_projection": False,
        "current_pointer_changed": False,
        "production_service_started": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_pause_released": False,
        "writer_quiescence_receipt_sha256": (
            writer_quiescence_receipt["receipt_sha256"]
            if writer_quiescence_receipt is not None
            else None
        ),
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.receipt.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
