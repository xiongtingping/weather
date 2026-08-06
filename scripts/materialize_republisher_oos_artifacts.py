#!/usr/bin/env python3
"""Build and optionally ingest canonical republisher historical OOS artifacts."""

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

from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.canonical_contract import (  # noqa: E402
    CanonicalRepublisherOOSArtifactContract,
    REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.republisher_oos_artifact import (  # noqa: E402
    build_republisher_oos_artifact_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


CONFIRMATION = "commit-republisher-oos-artifacts-production-no-live"


def _mapping(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(
        path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES
    )
    if not isinstance(value, Mapping):
        raise SystemExit("json_mapping_required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--builder-id", required=True)
    parser.add_argument("--station", action="append")
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--commit-production-no-live-artifacts", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    commit = bool(args.commit_production_no_live_artifacts)
    if commit and args.writer_quiescence_receipt is None:
        parser.error(
            "--commit-production-no-live-artifacts requires "
            "--writer-quiescence-receipt"
        )
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not commit and (
        args.writer_quiescence_receipt is not None or args.confirm is not None
    ):
        parser.error(
            "quiescence and confirmation arguments are valid only with commit"
        )

    database = args.database.expanduser()
    if database.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = database.resolve(strict=True)
    if not database.is_file():
        raise SystemExit("database_file_required")
    report_paths = [path.expanduser().resolve(strict=True) for path in args.report]
    reports = [_mapping(path) for path in report_paths]

    if commit:
        quiescence = _mapping(args.writer_quiescence_receipt)
        validate_writer_quiescence_receipt(
            quiescence,
            db_path=database,
            require_current_database_identity=True,
        )

    with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        if not commit:
            conn.execute("PRAGMA query_only=ON")
        packets = [
            build_republisher_oos_artifact_contract(
                report,
                conn,
                builder_id=args.builder_id,
                station_ids=args.station,
            )
            for report in reports
        ]
        as_of_values = {packet["as_of_utc"] for packet in packets}
        if len(as_of_values) != 1:
            raise SystemExit("report_as_of_mismatch")
        identity = {
            "schema": REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
            "as_of_utc": next(iter(as_of_values)),
            "builder_id": args.builder_id,
            "artifact_facts": [
                fact
                for packet in packets
                for fact in packet["artifact_facts"]
            ],
        }
        packet = CanonicalRepublisherOOSArtifactContract.from_mapping(
            {**identity, "content_sha256": content_sha256(identity)}
        ).to_mapping()
        receipt = ingest_canonical_contract(conn, packet, commit=commit)

    contract_output = args.contract_output.expanduser().resolve()
    receipt_output = args.receipt_output.expanduser().resolve()
    write_bounded_json(contract_output, packet, limit=MAX_JSON_INPUT_BYTES)
    wrapper = {
        "schema": "republisher_oos_artifact_materialization_receipt_v1",
        "state": receipt["state"],
        "database": str(database),
        "reports": [str(path) for path in report_paths],
        "contract_output": str(contract_output),
        "contract_content_sha256": packet["content_sha256"],
        "artifact_count": len(packet["artifact_facts"]),
        "ingress_receipt": receipt,
        "production_no_live_only": True,
        "native_arrival_written": False,
        "p1_written": False,
        "formal_live_authority_written": False,
        "selection_written": False,
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
