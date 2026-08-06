#!/usr/bin/env python3
"""Build and optionally ingest canonical republisher cross-selections."""

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
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.republisher_cross_selection import (  # noqa: E402
    build_republisher_cross_selection_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


CONFIRMATION = "commit-republisher-cross-selections-production-no-live"
_CANONICAL_ZERO_TYPES = (
    "native_arrival",
    "ensemble_member_receipt_manifest",
    "ensemble_member_arrival_binding",
    "activation",
)
_FORBIDDEN_TABLES = (
    "calibration_native_ensemble_activation",
    "calibration_cross_native_ensemble_artifact",
    "calibration_cross_native_ensemble_oos_prediction",
    "calibration_republisher_ensemble_activation",
    "basket_probability_envelope",
    "basket_p0_preselection_plan",
)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    result = {
        f"canonical:{fact_type}": int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
                (fact_type,),
            ).fetchone()[0]
        )
        for fact_type in _CANONICAL_ZERO_TYPES
    }
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in _FORBIDDEN_TABLES:
        result[f"table:{table}"] = (
            int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if table in tables
            else 0
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--builder-id", required=True)
    parser.add_argument("--station", action="append", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--commit-production-no-live-selection", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    commit = bool(args.commit_production_no_live_selection)
    if commit and args.writer_quiescence_receipt is None:
        parser.error(
            "--commit-production-no-live-selection requires --writer-quiescence-receipt"
        )
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not commit and (
        args.writer_quiescence_receipt is not None or args.confirm is not None
    ):
        parser.error("quiescence and confirmation are valid only with commit")

    supplied = args.database.expanduser()
    if supplied.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = supplied.resolve(strict=True)
    if not database.is_file():
        raise SystemExit("database_file_required")
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

    with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        if not commit:
            conn.execute("PRAGMA query_only=ON")
        before = _counts(conn)
        packet = build_republisher_cross_selection_contract(
            conn,
            station_ids=args.station,
            as_of_utc=args.as_of_utc,
            builder_id=args.builder_id,
        )
        receipt = ingest_canonical_contract(conn, packet, commit=commit)
        after = _counts(conn)
    if after != before:
        raise SystemExit("republisher_cross_selection_forbidden_surface_changed")

    contract_output = args.contract_output.expanduser().resolve()
    receipt_output = args.receipt_output.expanduser().resolve()
    write_bounded_json(contract_output, packet, limit=MAX_JSON_INPUT_BYTES)
    wrapper = {
        "schema": "republisher_cross_selection_materialization_receipt_v1",
        "state": receipt["state"],
        "database": str(database),
        "contract_output": str(contract_output),
        "contract_content_sha256": packet["content_sha256"],
        "selection_count": len(packet["selection_facts"]),
        "ingress_receipt": receipt,
        "forbidden_surface_counts_before": before,
        "forbidden_surface_counts_after": after,
        "production_no_live_only": True,
        "native_arrival_written": False,
        "p1_written": False,
        "formal_live_authority_written": False,
        "activation_written": False,
        "p0_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    wrapper = {**wrapper, "content_sha256": content_sha256(wrapper)}
    write_bounded_json(receipt_output, wrapper, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps(wrapper, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
