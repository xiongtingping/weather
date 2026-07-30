#!/usr/bin/env python3
"""Build RKPK rolling-origin P0-M predictions for TAF calibration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.taf_nwp_crossfit import build_nwp_crossfit_predictions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--taf-dataset", type=Path, required=True)
    parser.add_argument("--historical-evidence", type=Path, action="append", required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument(
        "--ensemble-source",
        type=Path,
        required=True,
        help="Formal ensemble artifact JSON or packet containing ensemble_artifacts.",
    )
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument(
        "--lead-bucket",
        choices=("d1", "d2", "d2_fallback"),
        required=True,
    )
    parser.add_argument(
        "--run-cycle",
        choices=("00Z", "06Z", "12Z", "18Z"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    taf = load_bounded_json(args.taf_dataset.expanduser().resolve())
    if not isinstance(taf, dict):
        raise SystemExit("TAF dataset must be a JSON object")
    ensemble_source = load_bounded_json(
        args.ensemble_source.expanduser().resolve()
    )
    if not isinstance(ensemble_source, dict):
        raise SystemExit("ensemble source must be a JSON object")
    candidates = ensemble_source.get("ensemble_artifacts")
    if candidates is None:
        candidates = [ensemble_source]
    if not isinstance(candidates, list):
        raise SystemExit("ensemble_artifacts must be a list")
    matches = [
        row
        for row in candidates
        if isinstance(row, dict)
        and str(row.get("station_id") or "").upper() == args.station_id.upper()
        and str(row.get("lead_bucket") or "") == args.lead_bucket
        and str(row.get("run_cycle") or "").upper() == args.run_cycle
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"ensemble source must resolve exactly one artifact; found {len(matches)}"
        )
    result = build_nwp_crossfit_predictions(
        taf_dataset=taf,
        evidence_paths=[path.expanduser().resolve() for path in args.historical_evidence],
        raw_root=args.raw_root.expanduser().resolve(),
        lead_bucket=args.lead_bucket,
        run_cycle=args.run_cycle,
        ensemble_artifact=matches[0],
        station_id=args.station_id.upper(),
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
