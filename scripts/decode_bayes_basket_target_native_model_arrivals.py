#!/usr/bin/env python3
"""CLI adapter for package-owned native model-arrival decoding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_arrival_decode import build  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DEFAULT_OUTPUT = (
    ROOT
    / "output/audits/bayes_basket_target_native_model_arrival_decode.json"
)

__all__ = ["build", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, action="append", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--market-registry-snapshot", type=Path, required=True)
    parser.add_argument("--grib-get")
    parser.add_argument("--cdo")
    parser.add_argument("--icon-grid", type=Path)
    parser.add_argument("--icon-weights", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(
        capture_paths=list(args.capture),
        as_of_utc=args.as_of_utc,
        market_registry_snapshot_path=args.market_registry_snapshot,
        grib_get_binary=args.grib_get,
        cdo_binary=args.cdo,
        icon_grid_path=args.icon_grid,
        icon_weights_path=args.icon_weights,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(json.dumps({
        "schema": result["schema"],
        "output": str(output),
        "decoded_point_count": result["decoded_point_count"],
        "complete_curve_count": result["curves"]["complete_curve_count"],
        "blocked_capture_count": len(result["blocked_captures"]),
        "promotion_allowed": result["checks"]["promotion_allowed"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
