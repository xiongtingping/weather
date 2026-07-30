#!/usr/bin/env python3
"""Build candidate-only nested OOS evidence for the actual live NWP stack."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.taf_live_combination_oos import (  # noqa: E402
    MIN_LIVE_COMBINATION_SAMPLE_N,
    MIN_NESTED_TRAIN_DAYS,
    build_taf_live_combination_oos_report,
)


DEFAULT_ROOT = ROOT / "output" / "audits" / "bayes_basket_target_kst_recalibration_20260727"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", "--exact-join", dest="observations", type=Path, default=DEFAULT_ROOT / "taf_exact_join_v2_RKPK_20260701_0725.json")
    parser.add_argument("--live-p0-inbox", type=Path, default=ROOT / "data" / "bayes_basket_target_p0_inbox.json")
    parser.add_argument("--allowed-evidence-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--season-bin", default="summer")
    parser.add_argument("--lead-bucket", default="d2_fallback")
    parser.add_argument("--run-cycle", default="18Z")
    parser.add_argument("--minimum-sample-n", type=int, default=MIN_LIVE_COMBINATION_SAMPLE_N)
    parser.add_argument("--minimum-train-days", type=int, default=MIN_NESTED_TRAIN_DAYS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_taf_live_combination_oos_report(observations_path=args.observations, live_p0_inbox_path=args.live_p0_inbox, allowed_evidence_root=args.allowed_evidence_root, station_id=args.station_id, season_bin=args.season_bin, lead_bucket=args.lead_bucket, run_cycle=args.run_cycle, minimum_sample_n=args.minimum_sample_n, minimum_train_days=args.minimum_train_days, output_path=args.output)
    print(json.dumps({"state": report["state"], "validated": report["validated"], "candidate_only": report["candidate_only"], "sample_n": report.get("sample_n"), "effective_sample_n": report.get("effective_sample_n"), "baseline_oos_metrics": report.get("baseline_oos_metrics"), "taf_live_combination_oos_metrics": report.get("taf_live_combination_oos_metrics"), "gaps": report.get("gaps"), "content_sha256": report["content_sha256"], "output": str(args.output.expanduser().resolve())}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
