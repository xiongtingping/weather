#!/usr/bin/env python3
"""Project native model-arrival evidence into one explicit station scope.

The source receipts may contain more stations than the isolated lane.  This
command keeps only the caller's allowlist, validates the original evidence
receipt before projection, and never turns a historical download or an
aggregator receipt into native arrival evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import load_model_arrival_receipts  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_model_arrival_evidence_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_model_arrival_evidence_awc_only.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope(station_ids: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted({str(value).strip().upper() for value in station_ids if str(value).strip()}))
    if not result:
        raise ContractError("model_arrival_scope_empty")
    return result


def _verify_source_receipt(payload: Mapping[str, Any]) -> None:
    supplied = str(payload.get("receipt_sha256") or "").strip()
    expected = content_sha256({key: value for key, value in payload.items() if key != "receipt_sha256"})
    if supplied != expected:
        raise ContractError("model_arrival_source_receipt_mismatch")


def build(*, input_paths: Sequence[Path], station_ids: Sequence[str], as_of_utc: str) -> dict[str, Any]:
    if not input_paths or len(input_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("model_arrival_input_batch_invalid")
    allowed = set(_scope(station_ids))
    selected: list[Mapping[str, Any]] = []
    input_artifacts: list[dict[str, str]] = []
    input_rows = 0
    excluded_rows = 0
    excluded_stations: set[str] = set()
    for raw_path in input_paths:
        path = raw_path.expanduser().resolve()
        payload = load_bounded_json(path)
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
            raise ContractError("model_arrival_source_schema_invalid")
        _verify_source_receipt(payload)
        arrivals = payload.get("arrivals")
        if not isinstance(arrivals, list):
            raise ContractError("model_arrival_source_arrivals_invalid")
        input_artifacts.append({"path": str(path), "sha256": _sha256(path)})
        for row in arrivals:
            if not isinstance(row, Mapping):
                raise ContractError("model_arrival_source_row_invalid")
            input_rows += 1
            station_id = str(row.get("station_id") or "").strip().upper()
            if station_id in allowed:
                selected.append(row)
            else:
                excluded_rows += 1
                if station_id:
                    excluded_stations.add(station_id)

    normalized, audit = load_model_arrival_receipts(selected, decision_at_utc=as_of_utc)
    if any(str(row.get("station_id") or "").strip().upper() not in allowed for row in normalized):
        raise ContractError("model_arrival_projected_scope_leak")
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "arrivals": normalized,
        "blocked_captures": [],
        "input_artifacts": input_artifacts,
        "scope": {
            "mode": "allowlist",
            "included_station_ids": sorted(allowed),
            "excluded_source_lanes": ["amsc"],
            "projection": "AWC_ONLY",
        },
        "projection_audit": {
            "input_file_count": len(input_paths),
            "input_row_count": input_rows,
            "selected_row_count": len(selected),
            "accepted_row_count": len(normalized),
            "excluded_row_count": excluded_rows,
            "excluded_station_ids": sorted(excluded_stations),
        },
        "audit": audit,
        "checks": {
            "arrival_count_positive": bool(normalized),
            "all_arrivals_prospective": bool(normalized) and all(row["capture_mode"] == "prospective" for row in normalized),
            "all_publication_sources_native": bool(normalized) and all(row["publication_time_source"] == "provider_http_last_modified" for row in normalized),
            "all_rows_in_explicit_awc_scope": all(str(row.get("station_id") or "").strip().upper() in allowed for row in normalized),
            "duplicate_key_free": audit["accepted_count"] == len(normalized),
            "promotion_allowed": False,
            "production_authority": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "boundary": {
            "target_only": True,
            "offline_evidence_projection": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    evidence_sha256 = content_sha256(identity)
    return {
        **identity,
        "evidence_sha256": evidence_sha256,
        "receipt_sha256": content_sha256({**identity, "evidence_sha256": evidence_sha256}),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(input_paths=args.input, station_ids=args.station_id, as_of_utc=args.as_of_utc)
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(json.dumps({
        "schema": SCHEMA,
        "output": str(output),
        "accepted_count": result["audit"]["accepted_count"],
        "excluded_row_count": result["projection_audit"]["excluded_row_count"],
        "scope_station_count": len(result["scope"]["included_station_ids"]),
        "promotion_allowed": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
