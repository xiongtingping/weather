#!/usr/bin/env python3
"""Single evaluation of frozen PR10 holdouts into selection and P0 roots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bayes_basket_target.canonical_ingress as ingress_module  # noqa: E402
from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.canonical_ssot_v15_migration import TARGET_TABLE_DDL, TARGET_TRIGGER_DDL  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_oos_builder import INPUT_FIELDS, build_model_oos_contract  # noqa: E402
from bayes_basket_target.model_oos_contract import CALIBRATION_METHOD_VERSION  # noqa: E402
from bayes_basket_target.model_oos_selection import build_historical_holdout_access_receipt, build_model_oos_selection_contract  # noqa: E402
from bayes_basket_target.p0_activation_root import build_p0_activation_root_contract  # noqa: E402
from bayes_basket_target.p0_calibrated_distribution import build_p0_calibrated_distribution_contract  # noqa: E402
from bayes_basket_target.resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json  # noqa: E402


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth-contract", type=Path, action="append", required=True)
    parser.add_argument("--event-bundle", type=Path, action="append", required=True)
    parser.add_argument("--reservation-bundle", type=Path, required=True)
    parser.add_argument("--evaluated-at-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    conn = _db()
    event_by_sha: dict[str, dict[str, object]] = {}
    try:
        for path in args.truth_contract:
            contract = load_bounded_json(path.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
            ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=str(contract["as_of_utc"]))
        for path in args.event_bundle:
            bundle = load_bounded_json(path.resolve(strict=True), limit=64 * 1024 * 1024)
            for contract in bundle["contracts"]:
                ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=str(contract["as_of_utc"]))
                for event in contract["event_facts"]:
                    event_by_sha[event["content_sha256"]] = event
        reserved = load_bounded_json(args.reservation_bundle.resolve(strict=True), limit=64 * 1024 * 1024)
        prior_v14_allowlist = ingress_module.V14_WRITER_ALLOWLIST
        prior_allowlist = ingress_module.V15_WRITER_ALLOWLIST
        enabled = {
            "p0_model_family_selection", "p0_calibrated_distribution", "p0_activation_root",
        }
        ingress_module.V14_WRITER_ALLOWLIST = frozenset(
            set(prior_v14_allowlist) | enabled
        )
        ingress_module.V15_WRITER_ALLOWLIST = frozenset(set(prior_allowlist) | {
            "p0_model_family_selection", "p0_calibrated_distribution", "p0_activation_root",
        })
        cohort_contracts = []
        access_receipts = []
        selections = []
        distributions = []
        activations = []
        terminals = []
        try:
            for reservation in reserved["reservations"]:
                access = build_historical_holdout_access_receipt(
                    reservation=reservation,
                    holdout_unsealed_at_utc=args.evaluated_at_utc,
                    first_evaluated_at_utc=args.evaluated_at_utc,
                    evaluator_id="pr10.reserved_holdout.single_evaluation.v1",
                )
                access_receipts.append(access)
                cohort_hashes = []
                for component in reservation["component_event_commitments"]:
                    hashes = [
                        *component["train_event_content_sha256s"],
                        *component["holdout_event_content_sha256s"],
                    ]
                    rows = [event_by_sha[item] for item in hashes]
                    inputs = [{
                        **{field: event[field] for field in INPUT_FIELDS if field != "model_receipt"},
                        "model_receipt": event["model_receipt"],
                    } for event in rows]
                    cohort = build_model_oos_contract(
                        conn,
                        as_of_utc=args.as_of_utc,
                        event_inputs=inputs,
                        cohort_spec={
                            "season_bin": reservation["season_bin"],
                            "calibration_method_version": CALIBRATION_METHOD_VERSION,
                            "train_event_dates": reservation["train_event_dates"],
                            "holdout_event_dates": reservation["holdout_event_dates"],
                        },
                    )
                    ingest_canonical_contract(conn, cohort, commit=True, ingested_at_utc=args.as_of_utc)
                    cohort_contracts.append(cohort)
                    cohort_hashes.append(cohort["cohort_facts"][0]["content_sha256"])
                try:
                    selection = build_model_oos_selection_contract(
                        conn,
                        cohort_content_sha256s=cohort_hashes,
                        pre_registered_mask_receipt=reservation,
                        holdout_label_access_receipt=access,
                        as_of_utc=args.as_of_utc,
                    )
                except ContractError as exc:
                    terminals.append({
                        "station_id": reservation["station_id"],
                        "lead_bucket": reservation["lead_bucket"],
                        "blocker": str(exc),
                    })
                    continue
                ingest_canonical_contract(conn, selection, commit=True, ingested_at_utc=args.as_of_utc)
                selections.append(selection)
                selection_fact = selection["selection_facts"][0]
                distribution = build_p0_calibrated_distribution_contract(
                    conn,
                    selection_content_sha256=selection_fact["content_sha256"],
                    as_of_utc=args.as_of_utc,
                )
                ingest_canonical_contract(conn, distribution, commit=True, ingested_at_utc=args.as_of_utc)
                distributions.append(distribution)
                activation = build_p0_activation_root_contract(
                    conn,
                    selection_content_sha256=selection_fact["content_sha256"],
                    calibrated_distribution_content_sha256=distribution["distribution_facts"][0]["content_sha256"],
                    activation_generation=1,
                    predecessor_activation_root_content_sha256=None,
                    as_of_utc=args.as_of_utc,
                )
                activations.append(activation)
        finally:
            ingress_module.V14_WRITER_ALLOWLIST = prior_v14_allowlist
            ingress_module.V15_WRITER_ALLOWLIST = prior_allowlist
        identity = {
            "schema": "bayes_basket_target_pr10_reserved_p0_bundle_v1",
            "as_of_utc": args.as_of_utc,
            "evaluated_at_utc": args.evaluated_at_utc,
            "parent_reservation_bundle_content_sha256": reserved["content_sha256"],
            "state": "MATERIALIZED_UNPUBLISHED" if activations else "BLOCKED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "reservation_count": len(reserved["reservations"]),
            "cohort_count": sum(len(item["cohort_facts"]) for item in cohort_contracts),
            "selection_count": len(selections),
            "p0_distribution_count": len(distributions),
            "p0_root_count": sum(len(item["activation_root_facts"]) for item in activations),
            "holdout_access_receipts": access_receipts,
            "cohort_contracts": cohort_contracts,
            "selection_contracts": selections,
            "distribution_contracts": distributions,
            "activation_contracts": activations,
            "terminals": terminals,
            "consumable": False,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "orders_submitted": 0,
        }
        output = {**identity, "content_sha256": content_sha256(identity)}
        write_bounded_json(args.output.resolve(), output, limit=64 * 1024 * 1024, max_nodes=2_000_000)
        print(json.dumps({key: output[key] for key in ("state", "reservation_count", "cohort_count", "selection_count", "p0_distribution_count", "p0_root_count", "content_sha256")}, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
