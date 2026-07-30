#!/usr/bin/env python3
"""Materialize only complete station-days from a partial exact-run receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


ALLOWED_SCHEMAS = {
    "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
    "bayes_basket_target_historical_regional_exact_run_evidence_v1",
}


def filter_success(*, input_path: Path, output_path: Path) -> dict[str, Any]:
    payload = load_bounded_json(input_path.resolve())
    if payload.get("schema") not in ALLOWED_SCHEMAS:
        raise ContractError("exact_run_filter_schema_invalid")
    blockers = payload.get("blockers")
    rows = payload.get("model_rows")
    stations = payload.get("station_ids")
    if (
        not isinstance(blockers, list)
        or not isinstance(rows, list)
        or not isinstance(stations, list)
    ):
        raise ContractError("exact_run_filter_shape_invalid")
    blocked_dates = {
        str(row.get("event_date") or "")
        for row in blockers
        if isinstance(row, Mapping) and str(row.get("event_date") or "")
    }
    retained = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("event_date") or "") not in blocked_dates
    ]
    retained_dates = sorted({
        str(row.get("event_date") or "") for row in retained
    })
    expected = len(stations) * len(retained_dates)
    if not retained_dates or len(retained) != expected:
        raise ContractError("exact_run_filter_retained_scope_incomplete")
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {
            "content_sha256",
            "blockers",
            "model_rows",
            "inputs",
            "checks",
            "start_date",
            "end_date",
        }
    }
    result_identity = {
        **identity,
        "start_date": retained_dates[0],
        "end_date": retained_dates[-1],
        "event_dates": retained_dates,
        "model_rows": retained,
        "inputs": [
            dict(row)
            for row in payload.get("inputs") or []
            if isinstance(row, Mapping)
            and str(row.get("event_date") or "") in retained_dates
        ],
        "blockers": [],
        "parent_partial_receipt": {
            "path": str(input_path.resolve()),
            "content_sha256": str(payload.get("content_sha256") or ""),
            "excluded_blocked_dates": sorted(blocked_dates),
            "excluded_blocker_count": len(blockers),
        },
        "checks": {
            "retained_station_dates_complete": True,
            "future_data_used": False,
            "parent_blockers_not_silently_reclassified": True,
        },
    }
    result = {
        **result_identity,
        "content_sha256": content_sha256(result_identity),
    }
    write_bounded_json(output_path, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = filter_success(
        input_path=args.input,
        output_path=args.output,
    )
    print(json.dumps({
        "schema": result["schema"],
        "model_id": result["model_id"],
        "event_date_count": len(result["event_dates"]),
        "model_row_count": len(result["model_rows"]),
        "excluded_blocked_dates":
            result["parent_partial_receipt"]["excluded_blocked_dates"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
