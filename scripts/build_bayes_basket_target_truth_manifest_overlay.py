#!/usr/bin/env python3
"""Overlay verified station truth datasets into a bounded 45-station manifest."""

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


SCHEMA = "bayes_basket_target_truth_manifest_overlay_v1"


def _overlays(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        station, separator, path = str(raw).partition("=")
        station = station.strip().upper()
        candidate = Path(path).expanduser().resolve()
        if not separator or not station or station in result:
            raise ContractError("truth_manifest_overlay_argument_invalid")
        result[station] = candidate
    return result


def build(
    *,
    base_path: Path,
    overlay_paths: Mapping[str, Path],
    output_path: Path,
) -> dict[str, Any]:
    base = load_bounded_json(base_path.resolve())
    rows = base.get("truth_results")
    if not isinstance(rows, list) or len(rows) != 45:
        raise ContractError("truth_manifest_overlay_base_scope_invalid")
    result_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ContractError("truth_manifest_overlay_base_row_invalid")
        station = str(raw.get("station_id") or "").strip().upper()
        if not station or station in seen:
            raise ContractError("truth_manifest_overlay_station_invalid")
        seen.add(station)
        dataset_path = overlay_paths.get(
            station,
            Path(str(raw.get("dataset") or "")).expanduser().resolve(),
        )
        dataset = load_bounded_json(dataset_path)
        if (
            dataset.get("schema")
            != "bayes_basket_station_calibration_dataset_v1"
            or str(dataset.get("station_id") or "").strip().upper()
            != station
        ):
            raise ContractError(
                f"truth_manifest_overlay_dataset_invalid:{station}"
            )
        samples = dataset.get("samples")
        missing = dataset.get("missing_dates")
        if not isinstance(samples, list) or not isinstance(missing, list):
            raise ContractError(
                f"truth_manifest_overlay_dataset_shape_invalid:{station}"
            )
        authority = str(dataset.get("label_authority") or "")
        if authority == "TEMPORARY_AWC_CALIBRATION_LABEL":
            raise ContractError(
                f"truth_manifest_overlay_temporary_awc_retired:{station}"
            )
        if authority != "FORMAL_STATION_LANE_TRUTH":
            raise ContractError(
                f"truth_manifest_overlay_authority_invalid:{station}"
            )
        result_rows.append({
            "station_id": station,
            "state": (
                "TRUTH_READY" if len(samples) >= 28 else "TRUTH_INSUFFICIENT"
            ),
            "sample_count": len(samples),
            "missing_date_count": len(missing),
            "label_authority": authority,
            "dataset": str(dataset_path),
        })
    if set(overlay_paths) - seen:
        raise ContractError("truth_manifest_overlay_unknown_station")
    identity = {
        "schema": SCHEMA,
        "station_count": len(result_rows),
        "source_manifest": str(base_path.resolve()),
        "source_manifest_content_sha256": str(
            base.get("content_sha256") or ""
        ),
        "overlay_station_ids": sorted(overlay_paths),
        "truth_results": sorted(
            result_rows, key=lambda row: row["station_id"]
        ),
        "checks": {
            "exact_45_station_scope": len(result_rows) == 45,
            "minimum_28_samples_ready_station_count": sum(
                row["state"] == "TRUTH_READY" for row in result_rows
            ),
            "future_truth_used": False,
        },
        "boundary": {
            "historical_only": True,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(output_path, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--overlay", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build(
        base_path=args.base,
        overlay_paths=_overlays(args.overlay),
        output_path=args.output,
    )
    print(json.dumps({
        "schema": result["schema"],
        "station_count": result["station_count"],
        "ready_station_count":
            result["checks"]["minimum_28_samples_ready_station_count"],
        "overlay_station_ids": result["overlay_station_ids"],
        "future_truth_used": False,
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
