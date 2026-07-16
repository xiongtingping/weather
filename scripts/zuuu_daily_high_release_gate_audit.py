#!/usr/bin/env python3
"""Read-only evidence-maturity audit for the guarded ZUUU daily-high rollout."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import audit_amsc_raw_history_archive
from scripts.build_trajectory_label_samples import DEFAULT_CUTOFFS, build_samples_from_db
from scripts.evaluate_trajectory_shadow import DEFAULT_BUCKETS
from trajectory_release_gate_runtime import build_release_gate_audit
from trajectory_shadow_evaluation_runtime import build_shadow_prediction, evaluate_shadow_predictions
from zuuu_daily_high_candidate_runtime import POLICY_VERSION as CANDIDATE_POLICY_VERSION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--cutoff-local", default=",".join(DEFAULT_CUTOFFS))
    parser.add_argument(
        "--frozen-registry",
        default=str(ROOT / "data" / "amsc_raw_history" / "governance" / "frozen_holdout_registry.json"),
        help="optional pre-registered frozen holdout registry; read-only",
    )
    return parser.parse_args()


def _coverage_event_dates(archive_dir: Path) -> list[str]:
    dates: set[str] = set()
    for path in archive_dir.glob("coverage_v2/*/*/*/*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        event_date = str(value.get("event_date") or "") if isinstance(value, dict) else ""
        if event_date:
            dates.add(event_date)
    return sorted(dates)


def _read_optional_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def main() -> int:
    args = parse_args()
    archive_dir = Path(args.archive_dir)
    cutoffs = [value.strip() for value in str(args.cutoff_local).split(",") if value.strip()]
    event_dates = _coverage_event_dates(archive_dir)
    sample_batch = build_samples_from_db(
        db_path=args.db,
        archive_dir=archive_dir,
        event_dates=event_dates,
        station_id=args.station_id,
        cutoff_locals=cutoffs,
    )
    predictions = []
    for sample in sample_batch["samples"]:
        baseline = sample.get("production_baseline") or {}
        baseline_map = baseline.get("likelihood_map") if isinstance(baseline, dict) and baseline.get("available") else {}
        buckets = list(baseline_map) if isinstance(baseline_map, dict) and baseline_map else list(DEFAULT_BUCKETS)
        predictions.append(build_shadow_prediction(sample, buckets=buckets, baseline_likelihood_map=baseline_map))
    evaluation = evaluate_shadow_predictions(predictions)
    raw_archive = audit_amsc_raw_history_archive(archive_dir)
    integrity_gates = {
        # Only the raw archive is evaluated in this standalone audit. The
        # remaining items are evidence-maturity inputs, not live rollout
        # blockers: per-snapshot runtime guards own the live fallback path.
        "hard_mask_violation_zero": False,
        "duplicate_valid_time_zero": False,
        "future_observation_leak_zero": False,
        "late_arrival_observation_leak_zero": False,
        "missing_default_fact_zero": False,
        "single_hpa_pressure_cutoff_false_positive_zero": False,
        "development_counterfactuals_pass": False,
        "ssot_audit_pass": False,
        "production_chain_audit_pass": False,
        "posterior_consistency_audit_pass": False,
        "raw_archive_integrity_pass": raw_archive.get("ok") is True,
    }
    report = build_release_gate_audit(
        samples=sample_batch["samples"],
        evaluation=evaluation,
        frozen_registry=_read_optional_json(Path(args.frozen_registry)),
        integrity_gates=integrity_gates,
        performance_gates={},
        candidate_policy_version=CANDIDATE_POLICY_VERSION,
        as_of=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    print(
        json.dumps(
            {
                "release_gate": report,
                "sample_manifest": sample_batch["manifest"],
                "evaluation": evaluation,
                "raw_archive": raw_archive,
                "integrity_evidence_scope": "raw_archive_only_other_named_evidence_maturity_audits_not_claimed_by_this_script",
                "guards": {
                    "writes_archive_db": False,
                    "writes_facts_sqlite": False,
                    "writes_posterior": False,
                    "places_orders": False,
                    "changes_parameters": False,
                    "releases_production_probability": False,
                    "a_day_count_blocks_live_rollout": False,
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
