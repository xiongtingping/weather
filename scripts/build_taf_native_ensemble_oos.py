#!/usr/bin/env python3
"""Build strict OOS TAF evidence for calibrated native-member P0."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    MIN_SAMPLE_N,
    build_taf_native_ensemble_oos_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exact-join", type=Path, required=True)
    parser.add_argument(
        "--live-p0-inbox",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_p0_inbox.json",
    )
    parser.add_argument("--taf-artifact", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--season-bin", required=True)
    parser.add_argument("--lead-bucket", required=True)
    parser.add_argument("--run-cycle", required=True)
    parser.add_argument(
        "--minimum-sample-n",
        type=int,
        default=MIN_SAMPLE_N,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_taf_native_ensemble_oos_report(
        exact_join_path=args.exact_join,
        live_p0_inbox_path=args.live_p0_inbox,
        taf_artifact_path=args.taf_artifact,
        station_id=args.station_id,
        season_bin=args.season_bin,
        lead_bucket=args.lead_bucket,
        run_cycle=args.run_cycle,
        minimum_sample_n=args.minimum_sample_n,
        output_path=args.output,
    )
    print(json.dumps({
        "state": report["state"],
        "validated": report["validated"],
        "sample_n": report.get("sample_n"),
        "live_fingerprint_sha256": (
            (report.get("live_fingerprint") or {}).get(
                "fingerprint_sha256"
            )
        ),
        "content_sha256": report["content_sha256"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
