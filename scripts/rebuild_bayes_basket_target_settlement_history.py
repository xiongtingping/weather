#!/usr/bin/env python3
"""Rebuild one registry-timezone historical truth/model artifact offline.

Inputs are retained exact arrival/decode evidence JSON objects.  This command
does not access the network or SQLite and writes only the final bounded
content-addressed evidence object.  Split mixed settlement timezones into
separate invocations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.historical_rebuild import rebuild_settlement_history  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    MAX_JSON_OUTPUT_BYTES,
    MIN_TARGET_FREE_BYTES,
    bounded_json_bytes,
    load_bounded_json,
    write_bounded_json,
)


def _rows_from_payload(payload: Any, *, kind: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        if any(key in payload for key in ("station_id", "arrival", "decode")):
            rows = [payload]
        else:
            keys = ("observations", "truth", "rows") if kind == "truth" else ("decoded_points", "points", "models", "rows")
            rows = next((payload.get(key) for key in keys if isinstance(payload.get(key), list)), None)
            if rows is None:
                raise ContractError(f"historical_{kind}_rows_missing")
    else:
        raise ContractError(f"historical_{kind}_payload_invalid")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ContractError(f"historical_{kind}_row_invalid")
    return [dict(row) for row in rows]


def _load_rows(paths: list[str], *, kind: str) -> list[dict[str, Any]]:
    if len(paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("historical_rebuild_input_file_budget_exceeded")
    result: list[dict[str, Any]] = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        payload = load_bounded_json(path)
        result.extend(_rows_from_payload(payload, kind=kind))
    return result


def _load_artifacts(paths: list[str]) -> list[dict[str, Any]]:
    if len(paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("historical_probability_artifact_file_budget_exceeded")
    result: list[dict[str, Any]] = []
    for value in paths:
        payload = load_bounded_json(Path(value).expanduser().resolve())
        if not isinstance(payload, Mapping):
            raise ContractError("historical_probability_artifact_mapping_required")
        result.append(dict(payload))
    return result


def _assert_output_capacity(path: Path, evidence: Mapping[str, Any]) -> None:
    raw = bounded_json_bytes(evidence, limit=MAX_JSON_OUTPUT_BYTES)
    candidate = path.expanduser().resolve().parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        free_bytes = int(shutil.disk_usage(candidate).free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise ContractError("historical_rebuild_disk_probe_unavailable") from exc
    if free_bytes < MIN_TARGET_FREE_BYTES + len(raw):
        raise ContractError("historical_rebuild_min_free_space_guard")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="content-addressed market registry snapshot")
    parser.add_argument("--truth", action="append", default=[], help="retained exact truth arrival/decode JSON; repeatable")
    parser.add_argument("--model", action="append", default=[], help="retained exact model arrival/decode JSON; repeatable")
    parser.add_argument("--probability-artifact", action="append", default=[], help="optional probability artifact JSON; repeatable")
    parser.add_argument("--station-id", action="append", required=True, help="registry station scope; repeatable")
    parser.add_argument("--event-date", action="append", required=True, help="registry settlement event date; repeatable")
    parser.add_argument("--cutoff-utc", required=True, help="only arrivals received at or before this UTC cutoff")
    parser.add_argument("--output", required=True, help="bounded rebuild evidence output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = load_market_registry_snapshot(Path(args.registry))
        truth_rows = _load_rows(args.truth, kind="truth")
        model_rows = _load_rows(args.model, kind="model")
        probability_rows = _load_artifacts(args.probability_artifact)
        evidence = rebuild_settlement_history(
            registry_rows=registry["rows"],
            truth_rows=truth_rows,
            model_rows=model_rows,
            probability_artifacts=probability_rows,
            cutoff_utc=args.cutoff_utc,
            station_ids=args.station_id,
            event_dates=args.event_date,
        )
        output_path = Path(args.output)
        _assert_output_capacity(output_path, evidence)
        write_bounded_json(output_path, evidence)
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"schema": "bayes_basket_target_settlement_history_rebuild_receipt_v1", "state": "FAIL_CLOSED", "reason": str(exc), "writes_production_database": False, "calls_order_gateway": False, "orders_submitted": 0}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({"schema": evidence["schema"], "state": "REBUILT", "output": str(Path(args.output).expanduser().resolve()), "evidence_sha256": evidence["evidence_sha256"], "truth_count": len(evidence["truth"]), "model_curve_count": len(evidence["model_curves"]), "calibration_eligible": any(item["calibration_eligible"] for item in evidence["exact_joins"]), "writes_production_database": False, "calls_order_gateway": False, "orders_submitted": 0}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
