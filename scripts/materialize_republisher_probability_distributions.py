#!/usr/bin/env python3
"""Build and optionally ingest train-only republisher probability artifacts."""

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
from bayes_basket_target.migration_quiescence import validate_writer_quiescence_receipt  # noqa: E402
from bayes_basket_target.republisher_no_live_p0 import build_probability_distribution_contract  # noqa: E402
from bayes_basket_target.resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json  # noqa: E402


CONFIRMATION = "commit-republisher-probability-distributions-production-no-live"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--selection-sha", action="append", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--commit-production-no-live", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    commit = bool(args.commit_production_no_live)
    if commit and args.writer_quiescence_receipt is None:
        parser.error("commit requires --writer-quiescence-receipt")
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    supplied = args.database.expanduser()
    if supplied.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = supplied.resolve(strict=True)
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
        before = int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE fact_type='probability_distribution_artifact'"
            ).fetchone()[0]
        )
        packet = build_probability_distribution_contract(
            conn,
            selection_content_sha256s=args.selection_sha,
            as_of_utc=args.as_of_utc,
        )
        ingress = ingest_canonical_contract(conn, packet, commit=commit)
        after = int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact "
                "WHERE fact_type='probability_distribution_artifact'"
            ).fetchone()[0]
        )
    expected_delta = int(ingress["inserted_count"]) if commit else 0
    if after - before != expected_delta:
        raise SystemExit("probability_distribution_count_mismatch")
    write_bounded_json(
        args.contract_output.expanduser().resolve(),
        packet,
        limit=MAX_JSON_INPUT_BYTES,
    )
    wrapper = {
        "schema": "republisher_probability_distribution_materialization_receipt_v1",
        "state": ingress["state"],
        "database": str(database),
        "contract_content_sha256": packet["content_sha256"],
        "artifact_count_before": before,
        "artifact_count_after": after,
        "ingress_receipt": ingress,
        "production_no_live_only": True,
        "uses_holdout_for_fit": False,
        "p0_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    wrapper = {**wrapper, "content_sha256": content_sha256(wrapper)}
    write_bounded_json(
        args.receipt_output.expanduser().resolve(),
        wrapper,
        limit=MAX_JSON_INPUT_BYTES,
    )
    print(json.dumps(wrapper, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
