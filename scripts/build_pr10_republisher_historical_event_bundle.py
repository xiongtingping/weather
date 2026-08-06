#!/usr/bin/env python3
"""Build PR10 fixed-lead MODEL_OOS events in an isolated canonical V15 SSOT."""

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
from bayes_basket_target.canonical_ssot_schema import inspect_canonical_ssot_schema  # noqa: E402
from bayes_basket_target.canonical_ssot_v15_migration import (  # noqa: E402
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_oos_builder import (  # noqa: E402
    INPUT_FIELDS,
    build_model_oos_contract,
)
from bayes_basket_target.model_oos_contract import (  # noqa: E402
    CALIBRATION_METHOD_VERSION,
)
from bayes_basket_target.model_oos_selection import (  # noqa: E402
    build_model_oos_selection_terminal_receipt,
)
from bayes_basket_target.republisher_historical_event_builder import (  # noqa: E402
    build_republisher_historical_event_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def _isolated_v15(truth_contract: dict[str, object]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    if inspect_canonical_ssot_schema(conn).get("state") != "V15":
        raise ContractError("pr10_isolated_v15_schema_invalid")
    receipt = ingest_canonical_contract(
        conn,
        truth_contract,
        commit=True,
        ingested_at_utc=str(truth_contract["as_of_utc"]),
    )
    if receipt["validated_counts"].get("truth") != len(
        truth_contract.get("truth_facts", [])
    ):
        raise ContractError("pr10_isolated_truth_ingress_count_mismatch")
    return conn


def _evidence_paths(root: Path) -> list[Path]:
    base = root.expanduser().resolve(strict=True)
    expected = sorted(coordinate_registry_manifest()["station_ids"])
    paths = [
        base / station / f"five_model_d{lead}_oos.json"
        for station in expected
        for lead in (1, 2)
    ]
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ContractError("pr10_republisher_exact_90_evidence_required")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth-contract", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort-output", type=Path)
    parser.add_argument("--selection-terminal-output", type=Path)
    args = parser.parse_args()

    truth_contract = load_bounded_json(
        args.truth_contract.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    coverage = load_bounded_json(
        args.coverage.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    projection = load_bounded_json(
        args.authority_projection.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    paths = _evidence_paths(args.evidence_root)
    evidences = [
        load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES) for path in paths
    ]
    rows = coverage.get("rows")
    if not isinstance(rows, list):
        raise ContractError("pr10_republisher_coverage_rows_missing")
    conn = _isolated_v15(truth_contract)
    cohort_bundle = None
    selection_terminal_bundle = None
    try:
        bundle = build_republisher_historical_event_bundle(
            conn,
            evidences=evidences,
            evidence_paths=paths,
            coverage_rows=rows,
            authority_scope_projection=projection,
            as_of_utc=args.as_of_utc,
        )
        cohort_contracts = []
        for contract in bundle["contracts"]:
            events = contract["event_facts"]
            if len(events) != 28:
                continue
            event_inputs = [
                {
                    **{
                        field: event[field]
                        for field in INPUT_FIELDS
                        if field != "model_receipt"
                    },
                    "model_receipt": event["model_receipt"],
                }
                for event in events
            ]
            dates = [str(event["event_date"]) for event in events]
            cohort_contract = build_model_oos_contract(
                conn,
                as_of_utc=args.as_of_utc,
                event_inputs=event_inputs,
                cohort_spec={
                    "season_bin": "JULY",
                    "calibration_method_version": CALIBRATION_METHOD_VERSION,
                    "train_event_dates": dates[:20],
                    "holdout_event_dates": dates[20:],
                },
            )
            ingest_canonical_contract(
                conn,
                cohort_contract,
                commit=True,
                ingested_at_utc=args.as_of_utc,
            )
            cohort_contracts.append(cohort_contract)
        cohort_identity = {
            "schema": "bayes_basket_target_pr10_exact_28_cohort_bundle_v1",
            "as_of_utc": args.as_of_utc,
            "builder_id": "pr10.republisher_exact_28_cohort.v1",
            "parent_event_bundle_content_sha256": bundle["content_sha256"],
            "state": "MATERIALIZED_UNPUBLISHED" if cohort_contracts else "BLOCKED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "partition_policy": "FIRST_20_TRAIN_LAST_8_HOLDOUT_FROM_EXACT_SORTED_28",
            "contract_count": len(cohort_contracts),
            "cohort_count": sum(
                len(contract["cohort_facts"]) for contract in cohort_contracts
            ),
            "oos_pass_count": sum(
                fact["evaluation_state"] == "OOS_PASS"
                for contract in cohort_contracts
                for fact in contract["cohort_facts"]
            ),
            "oos_rejected_count": sum(
                fact["evaluation_state"] == "OOS_REJECTED"
                for contract in cohort_contracts
                for fact in contract["cohort_facts"]
            ),
            "contracts": cohort_contracts,
            "writes_database": False,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "orders_submitted": 0,
            "consumable": False,
        }
        cohort_bundle = {
            **cohort_identity,
            "content_sha256": content_sha256(cohort_identity),
        }
        grouped_cohort_hashes: dict[tuple[object, ...], list[str]] = {}
        selection_scope_fields = (
            "station_id", "settlement_timezone", "lead_bucket",
            "lead_bucket_scope", "run_cycle", "run_cycle_scope",
            "season_bin", "source_class", "authority_scope",
            "event_oos_protocol_version", "calibration_method_version",
        )
        for contract in cohort_contracts:
            for cohort in contract["cohort_facts"]:
                scope = tuple(cohort[field] for field in selection_scope_fields)
                grouped_cohort_hashes.setdefault(scope, []).append(
                    cohort["content_sha256"]
                )
        terminals = [
            build_model_oos_selection_terminal_receipt(
                conn,
                cohort_content_sha256s=hashes,
                pre_registered_mask_receipt=None,
                as_of_utc=args.as_of_utc,
            )
            for _scope, hashes in sorted(
                grouped_cohort_hashes.items(), key=lambda item: str(item[0])
            )
        ]
        selection_terminal_identity = {
            "schema": (
                "bayes_basket_target_pr10_model_oos_selection_"
                "terminal_bundle_v1"
            ),
            "as_of_utc": args.as_of_utc,
            "builder_id": "pr10.model_oos_selection_terminal.v1",
            "parent_cohort_bundle_content_sha256": cohort_bundle[
                "content_sha256"
            ],
            "state": "BLOCKED",
            "blocker": "PRE_REGISTERED_MASK_RECEIPT_MISSING",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "terminal_count": len(terminals),
            "selection_contract_count": 0,
            "terminals": terminals,
            "writes_database": False,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "consumable": False,
        }
        selection_terminal_bundle = {
            **selection_terminal_identity,
            "content_sha256": content_sha256(selection_terminal_identity),
        }
    finally:
        conn.close()
    write_bounded_json(
        args.output.expanduser().resolve(),
        bundle,
        limit=64 * 1024 * 1024,
        max_nodes=2_000_000,
    )
    if args.cohort_output is not None:
        write_bounded_json(
            args.cohort_output.expanduser().resolve(),
            cohort_bundle,
            limit=64 * 1024 * 1024,
            max_nodes=2_000_000,
        )
    if args.selection_terminal_output is not None:
        write_bounded_json(
            args.selection_terminal_output.expanduser().resolve(),
            selection_terminal_bundle,
            limit=64 * 1024 * 1024,
            max_nodes=2_000_000,
        )
    print(json.dumps({
        "state": bundle["state"],
        "evidence_count": bundle["evidence_count"],
        "expected_scope_count": bundle["expected_scope_count"],
        "event_count": bundle["event_count"],
        "cohort_count": bundle["cohort_count"],
        "terminal_count": bundle["terminal_count"],
        "blocker_counts": bundle["blocker_counts"],
        "content_sha256": bundle["content_sha256"],
        "exact_28_cohort_count": cohort_bundle["cohort_count"],
        "oos_pass_count": cohort_bundle["oos_pass_count"],
        "oos_rejected_count": cohort_bundle["oos_rejected_count"],
        "selection_terminal_count": selection_terminal_bundle[
            "terminal_count"
        ],
        "writes_production_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
