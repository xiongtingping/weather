#!/usr/bin/env python3
"""Project retained gap evidence into the one canonical gap ledger.

This command is read-only.  It never fetches a provider, changes SSOT, or
turns a forward row into OOS.  Input records must already be canonical ledger
records; aliases, omitted fields and duplicate scopes fail closed.  Any
legacy report must be converted by a separately named, reviewed adapter before
it reaches this command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import CanonicalContract
from bayes_basket_target.gap_ledger import (
    GapRecord,
    build_gap_ledger,
    gap_records_from_canonical_contract,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json


CANONICAL_RECORD_FIELDS = frozenset(
    {
        "station_id", "model_id", "model_family", "run_cycle", "lead_bucket",
        "event_date", "evidence_role", "native_arrival_verified",
        "raw_receipt_hash_complete", "awc_truth_closed", "taf_revision_complete",
        "train_count", "holdout_count", "state", "blockers", "evidence",
        "content_sha256",
    }
)


def _project(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("canonical_gap_projection_record_mapping_required")
    unknown = sorted(set(raw) - CANONICAL_RECORD_FIELDS)
    missing = sorted(CANONICAL_RECORD_FIELDS - set(raw))
    if unknown:
        raise ValueError(f"canonical_gap_projection_unknown_field:{','.join(unknown)}")
    if missing:
        raise ValueError(f"canonical_gap_projection_missing_field:{','.join(missing)}")
    # The ledger is already canonical.  It is not a compatibility layer for
    # the old diagnostic names (lead, diagnostic_blockers, or omitted fields).
    # Parsing through GapRecord rechecks types, hashes, family and lead scope.
    return GapRecord.from_mapping(raw).to_mapping()


def build(input_path: Path, *, as_of_utc: str, output: Path) -> dict[str, Any]:
    source = load_bounded_json(input_path)
    records = source.get("records")
    if not isinstance(records, list):
        raise ValueError("canonical_gap_projection_records_missing")
    if any(not isinstance(item, Mapping) for item in records):
        raise ValueError("canonical_gap_projection_record_mapping_required")
    projected = [_project(item) for item in records]
    result = build_gap_ledger(
        projected,
        as_of_utc=as_of_utc,
        source_pointers={"legacy_ledger": str(input_path.resolve()), "legacy_content_sha256": str(source.get("content_sha256") or "")},
    )
    write_bounded_json(output, result)
    return result


def build_from_contracts(
    contract_paths: list[Path],
    *,
    as_of_utc: str,
    output: Path,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
) -> dict[str, Any]:
    """Project only validated canonical contracts into the single ledger.

    The train/holdout split is an explicit builder output; it is required at
    this boundary rather than inferred from dates or broadcast from a legacy
    report.  Every contract is parsed by the same canonical schema before any
    row is emitted.
    """

    if not contract_paths:
        raise ValueError("canonical_gap_contract_paths_missing")
    records: list[dict[str, Any]] = []
    pointers: dict[str, str] = {}
    for index, path in enumerate(contract_paths):
        source = load_bounded_json(path)
        contract = CanonicalContract.from_mapping(source)
        records.extend(
            gap_records_from_canonical_contract(
                contract.to_mapping(),
                train_count=train_count,
                holdout_count=holdout_count,
                taf_revision_complete=taf_revision_complete,
            )
        )
        pointers[f"contract_{index}_sha256"] = contract.content_sha256
        pointers[f"contract_{index}_path"] = str(path.resolve())
    result = build_gap_ledger(
        records,
        as_of_utc=as_of_utc,
        source_pointers=pointers,
    )
    write_bounded_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--contract", type=Path, action="append")
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--train-count", type=int)
    parser.add_argument("--holdout-count", type=int)
    parser.add_argument("--taf-revision-complete", action="store_true")
    args = parser.parse_args()
    if args.contract:
        if args.train_count is None or args.holdout_count is None:
            parser.error("--train-count and --holdout-count are required with --contract")
        result = build_from_contracts(
            [path.expanduser().resolve(strict=True) for path in args.contract],
            as_of_utc=args.as_of_utc,
            output=args.output.expanduser().resolve(),
            train_count=args.train_count,
            holdout_count=args.holdout_count,
            taf_revision_complete=bool(args.taf_revision_complete),
        )
    else:
        result = build(args.input.expanduser().resolve(strict=True), as_of_utc=args.as_of_utc, output=args.output.expanduser().resolve())
    print(json.dumps({"schema": result["schema"], "record_count": len(result["records"]), "content_sha256": result["content_sha256"], "output": str(args.output)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
