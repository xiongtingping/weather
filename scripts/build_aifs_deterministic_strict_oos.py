#!/usr/bin/env python3
"""Materialize a bounded, read-only AIFS Single strict-OOS status receipt.

The official AIFS Single history is a deterministic, native ECMWF product.
This command consumes only a previously content-addressed AIFS training batch
or its read-only training-facts runner receipt.  It never fetches a model
object, opens SQLite, creates provider/P0 authority, or writes a production
table.  Before the formal 20 train + 8 holdout scope is present it emits an
explicit, content-addressed shortfall receipt rather than pretending that a
strict-OOS artifact exists.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.aifs_deterministic_oos import (  # noqa: E402
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    TRAINING_BATCH_SCHEMA,
    _p0_scope_for_fact,
    build_aifs_deterministic_strict_oos_candidate,
    validate_aifs_deterministic_training_batch,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "aifs_deterministic_strict_oos_runner_receipt_v1"
TRAINING_RECEIPT_SCHEMA = "aifs_deterministic_training_facts_runner_receipt_v1"
DEFAULT_TRAINING_INPUT = (
    ROOT / "output/audits/aifs_training_facts_45station_closed_inventory_20260802_latest.json"
)
DEFAULT_OUTPUT = ROOT / "output/audits/aifs_deterministic_strict_oos_20260802_latest.json"


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("aifs_deterministic_oos_runner_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("aifs_deterministic_oos_runner_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _identity_sha(value: Mapping[str, Any], *, label: str) -> str:
    supplied = str(value.get("content_sha256") or "").strip().lower()
    if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
        raise ContractError(f"aifs_deterministic_oos_runner_{label}_sha_invalid")
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"aifs_deterministic_oos_runner_{label}_sha_mismatch")
    return supplied


def _training_batch(input_value: Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """Return a verified batch and an optional enclosing runner receipt hash."""

    if input_value.get("schema") == TRAINING_BATCH_SCHEMA:
        _identity_sha(input_value, label="training_batch")
        return input_value, None
    if input_value.get("schema") != TRAINING_RECEIPT_SCHEMA:
        raise ContractError("aifs_deterministic_oos_runner_training_input_schema_invalid")
    receipt_sha = _identity_sha(input_value, label="training_receipt")
    batch = input_value.get("training_batch")
    if not isinstance(batch, Mapping):
        raise ContractError("aifs_deterministic_oos_runner_training_batch_missing")
    _identity_sha(batch, label="training_batch")
    if input_value.get("training_batch_content_sha256") != batch.get("content_sha256"):
        raise ContractError("aifs_deterministic_oos_runner_training_batch_pointer_invalid")
    return batch, receipt_sha


def _scope_shortfalls(facts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Give exact 20+8 shortages without making a non-OOS prediction."""

    groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for fact in facts:
        season_bin, lead_bucket = _p0_scope_for_fact(fact)
        groups[(
            str(fact["station_id"]),
            str(fact["target_timezone"]),
            str(fact["run_cycle"]),
            season_bin,
            lead_bucket,
        )].append(fact)
    required = MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS
    rows: list[dict[str, Any]] = []
    for scope, values in sorted(groups.items()):
        event_dates = sorted({str(item["event_date"]) for item in values})
        if len(event_dates) != len(values):
            # The core validator already rejects duplicate station-days.  This
            # turns any unexpected mutation into an explicit fail-closed error.
            raise ContractError("aifs_deterministic_oos_runner_duplicate_event_date")
        rows.append({
            "station_id": scope[0],
            "target_timezone": scope[1],
            "run_cycle": scope[2],
            "season_bin": scope[3],
            "lead_bucket": scope[4],
            "available_case_count": len(values),
            "required_train_case_count": MIN_TRAIN_DAYS,
            "required_strict_holdout_case_count": MIN_HOLDOUT_DAYS,
            "required_total_case_count": required,
            "missing_case_count": max(0, required - len(values)),
            "event_date_start": event_dates[0],
            "event_date_end": event_dates[-1],
        })
    return rows


def build_strict_oos_runner_receipt(
    training_input: Mapping[str, Any], *, as_of_utc: str
) -> dict[str, Any]:
    """Build either a strict-OOS report or a formally non-authorizing gap receipt."""

    assert_process_memory_budget()
    as_of = _utc(as_of_utc)
    batch, source_receipt_sha = _training_batch(training_input)
    facts = validate_aifs_deterministic_training_batch(batch)
    boundary = {
        "reads_training_artifact_only": True,
        "opens_sqlite": False,
        "downloads_raw": False,
        "writes_production_database": False,
        "writes_native_ensemble_tables": False,
        "creates_formal_provider_binding": False,
        "activates_p0": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }
    common = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "training_batch_content_sha256": batch["content_sha256"],
        "training_receipt_content_sha256": source_receipt_sha,
        "fact_count": len(facts),
        "formal_p0_eligible": False,
        "boundary": boundary,
    }
    try:
        report = build_aifs_deterministic_strict_oos_candidate(batch)
    except ContractError as exc:
        if str(exc) != "aifs_deterministic_oos_insufficient_samples":
            raise
        shortfalls = _scope_shortfalls(facts)
        identity = {
            **common,
            "state": "PENDING_STRICT_OOS_SAMPLES",
            "strict_oos_report": None,
            "candidate_count": 0,
            "scope_shortfalls": shortfalls,
            "blockers": ["样本不足"],
        }
    else:
        identity = {
            **common,
            "state": "STRICT_OOS_CANDIDATES_NOT_BINDING",
            "strict_oos_report": report,
            "candidate_count": int(report["candidate_count"]),
            "scope_shortfalls": [],
            "blockers": [],
        }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-input", type=Path, default=DEFAULT_TRAINING_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    args = parser.parse_args()
    value = load_bounded_json(
        args.training_input.expanduser().resolve(strict=True),
        limit=MAX_JSON_INPUT_BYTES,
    )
    if not isinstance(value, Mapping):
        raise ContractError("aifs_deterministic_oos_runner_training_input_mapping_required")
    receipt = build_strict_oos_runner_receipt(value, as_of_utc=args.as_of_utc)
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, receipt, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "schema": receipt["schema"],
        "state": receipt["state"],
        "fact_count": receipt["fact_count"],
        "candidate_count": receipt["candidate_count"],
        "scope_shortfall_count": len(receipt["scope_shortfalls"]),
        "formal_p0_eligible": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "output": str(output),
        "wrote": wrote,
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
