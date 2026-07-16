#!/usr/bin/env python3
"""Run read-only, event-day-weighted evaluation of ZUUU trajectory candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_trajectory_label_samples import DEFAULT_CUTOFFS, build_samples_from_db
from trajectory_shadow_evaluation_runtime import build_shadow_prediction, evaluate_shadow_predictions


DEFAULT_BUCKETS = (
    "32°C or below",
    "33°C",
    "34°C",
    "35°C",
    "36°C",
    "37°C",
    "38°C",
    "39°C",
    "40°C",
    "41°C",
    "42°C or higher",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append", required=True, help="station-local YYYY-MM-DD; may be repeated")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument("--cutoff-local", default=",".join(DEFAULT_CUTOFFS))
    parser.add_argument("--buckets", default=",".join(DEFAULT_BUCKETS), help="fallback bucket labels when a persisted baseline is unavailable")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cutoffs = [value.strip() for value in str(args.cutoff_local).split(",") if value.strip()]
    fallback_buckets = [value.strip() for value in str(args.buckets).split(",") if value.strip()]
    if not cutoffs or not fallback_buckets:
        print("--cutoff-local and --buckets must be non-empty", file=sys.stderr)
        return 2
    sample_batch = build_samples_from_db(
        db_path=args.db,
        archive_dir=args.archive_dir,
        event_dates=list(args.event_date),
        station_id=args.station_id,
        cutoff_locals=cutoffs,
    )
    predictions = []
    for sample in sample_batch["samples"]:
        baseline = sample.get("production_baseline") or {}
        baseline_map = baseline.get("likelihood_map") if isinstance(baseline, dict) and baseline.get("available") else {}
        buckets = list(baseline_map) if isinstance(baseline_map, dict) and baseline_map else fallback_buckets
        predictions.append(build_shadow_prediction(sample, buckets=buckets, baseline_likelihood_map=baseline_map))
    evaluation = evaluate_shadow_predictions(predictions)
    print(
        json.dumps(
            {
                "sample_manifest": sample_batch["manifest"],
                "evaluation": evaluation,
                "prediction_count": len(predictions),
                "guards": {
                    "writes_archive_db": False,
                    "writes_facts_sqlite": False,
                    "writes_posterior": False,
                    "places_orders": False,
                    "changes_parameters": False,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
