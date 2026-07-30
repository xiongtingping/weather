#!/usr/bin/env python3
"""Build strict-time crossfit→live TAF transport OOS evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.taf_nwp_transport_oos import (  # noqa: E402
    MIN_TRANSPORT_SAMPLE_N,
    build_transport_oos_report,
)


DEFAULT_RECALIBRATION_ROOT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_kst_recalibration_20260727"
)
DEFAULT_HISTORY_PREFIX_TAF = (
    DEFAULT_RECALIBRATION_ROOT
    / "awc_taf_calibration_RKPK_20260628_0630.json"
)
DEFAULT_HISTORY_PREFIX_EVIDENCE = (
    DEFAULT_RECALIBRATION_ROOT
    / "ecmwf_18z_d2_kst_RKPK_backfill_20260628_0713.json",
    DEFAULT_RECALIBRATION_ROOT
    / "gfs_18z_d2_kst_RKPK_backfill_20260628_0715.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exact-join",
        type=Path,
        default=(
            DEFAULT_RECALIBRATION_ROOT
            / "taf_exact_join_v2_RKPK_20260701_0725.json"
        ),
    )
    parser.add_argument(
        "--crossfit",
        type=Path,
        default=(
            DEFAULT_RECALIBRATION_ROOT
            / "taf_nwp_crossfit_RKPK_20260628_0725.json"
        ),
    )
    parser.add_argument(
        "--live-p0-inbox",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_p0_inbox.json",
    )
    parser.add_argument(
        "--taf-artifact",
        type=Path,
        default=(
            ROOT / "config" / "artifacts"
            / "bayes_basket_target_taf_revision_active.json"
        ),
    )
    parser.add_argument(
        "--allowed-evidence-root",
        type=Path,
        default=DEFAULT_RECALIBRATION_ROOT,
    )
    parser.add_argument(
        "--history-prefix-taf",
        type=Path,
        default=DEFAULT_HISTORY_PREFIX_TAF,
    )
    parser.add_argument(
        "--history-prefix-evidence",
        type=Path,
        action="append",
        default=None,
    )
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--season-bin", default="summer")
    parser.add_argument("--lead-bucket", default="d2_fallback")
    parser.add_argument("--run-cycle", default="18Z")
    parser.add_argument(
        "--minimum-sample-n",
        type=int,
        default=MIN_TRANSPORT_SAMPLE_N,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    history_prefix_evidence = (
        tuple(args.history_prefix_evidence)
        if args.history_prefix_evidence
        else DEFAULT_HISTORY_PREFIX_EVIDENCE
    )

    report = build_transport_oos_report(
        exact_join_path=args.exact_join,
        crossfit_path=args.crossfit,
        live_p0_inbox_path=args.live_p0_inbox,
        taf_artifact_path=args.taf_artifact,
        allowed_evidence_root=args.allowed_evidence_root,
        station_id=args.station_id,
        season_bin=args.season_bin,
        lead_bucket=args.lead_bucket,
        run_cycle=args.run_cycle,
        history_prefix_taf_path=args.history_prefix_taf,
        history_prefix_evidence_paths=history_prefix_evidence,
        minimum_sample_n=args.minimum_sample_n,
    )
    write_bounded_json(args.output, report)
    print(json.dumps({
        "state": report["state"],
        "validated": report["validated"],
        "sample_n": report.get("sample_n"),
        "effective_sample_n": report.get("effective_sample_n"),
        "baseline_oos_metrics": report.get("baseline_oos_metrics"),
        "transport_oos_metrics": report.get("transport_oos_metrics"),
        "gaps": report.get("gaps"),
        "content_sha256": report["content_sha256"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
