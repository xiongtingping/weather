#!/usr/bin/env python3
"""
Feedback loop runner (SSOT writer).

Writes calibration/stats snapshots into data/archive.db:
- edge_bias_calibration_runs / edge_bias_calibration_bins (used by scan/auto-trade)
- feedback_snapshots (model_bias / place_bias / prediction_hit_stats / source_lead_day_hit_rates / strategy_stats / entry_counterfactual_review status)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


LEGACY_OPTIMIZER_APPLY_RETIRED_REASON = "legacy_optimizer_apply_retired_use_parameter_change_governance"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-bias-days", type=int, default=int(os.environ.get("PM_EDGE_BIAS_DAYS", "30") or 30))
    ap.add_argument("--model-bias-days", type=int, default=int(os.environ.get("PM_MODEL_BIAS_DAYS", "7") or 7))
    ap.add_argument("--place-bias-days", type=int, default=int(os.environ.get("PM_PLACE_BIAS_DAYS", "7") or 7))
    ap.add_argument("--hit-stats-days", type=str, default=os.environ.get("PM_HIT_STATS_DAYS", "30,0") or "30,0")
    ap.add_argument("--strategy-days", type=int, default=int(os.environ.get("PM_STRATEGY_STATS_DAYS", "30") or 30))
    ap.add_argument("--orphan-fill-days", type=int, default=int(os.environ.get("PM_ORPHAN_FILL_STATS_DAYS", "60") or 60))
    ap.add_argument("--end-date", type=str, default=os.environ.get("PM_FEEDBACK_END_DATE", "") or "")
    ap.add_argument("--apply", action="store_true", help="Retired; kept for compatibility and never applies config")
    args = ap.parse_args()

    # Import from repo root (script may be launched from launchd with different cwd).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from ui.server import run_feedback_loop_once

    payload = run_feedback_loop_once(
        edge_bias_days=int(args.edge_bias_days),
        model_bias_days=int(args.model_bias_days),
        place_bias_days=int(args.place_bias_days),
        hit_stats_days=str(args.hit_stats_days or ""),
        strategy_days=int(args.strategy_days),
        orphan_fill_days=int(args.orphan_fill_days),
        end_date=str(args.end_date or ""),
    )
    if args.apply:
        payload["auto_apply"] = {
            "ok": True,
            "enabled": False,
            "applied_any": False,
            "status": "retired_no_write",
            "reason_code": LEGACY_OPTIMIZER_APPLY_RETIRED_REASON,
            "next_boundary": "create_parameter_change_request_from_approved_calibration_candidate",
            "guards": {
                "writes_archive_db": False,
                "writes_config_files": False,
                "changes_parameters": False,
                "restarts_launchd": False,
                "places_orders": False,
            },
        }
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
