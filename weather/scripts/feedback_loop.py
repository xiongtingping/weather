#!/usr/bin/env python3
"""
Feedback loop runner (SSOT writer).

Writes calibration/stats snapshots into data/archive.db:
- edge_bias_calibration_runs / edge_bias_calibration_bins (used by scan/auto-trade)
- feedback_snapshots (model_bias / place_bias / strategy_stats)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-bias-days", type=int, default=int(os.environ.get("PM_EDGE_BIAS_DAYS", "30") or 30))
    ap.add_argument("--model-bias-days", type=int, default=int(os.environ.get("PM_MODEL_BIAS_DAYS", "7") or 7))
    ap.add_argument("--place-bias-days", type=int, default=int(os.environ.get("PM_PLACE_BIAS_DAYS", "7") or 7))
    ap.add_argument("--strategy-days", type=int, default=int(os.environ.get("PM_STRATEGY_STATS_DAYS", "30") or 30))
    ap.add_argument("--orphan-fill-days", type=int, default=int(os.environ.get("PM_ORPHAN_FILL_STATS_DAYS", "60") or 60))
    ap.add_argument("--end-date", type=str, default=os.environ.get("PM_FEEDBACK_END_DATE", "") or "")
    ap.add_argument("--apply", action="store_true", help="Apply optimizer suggestions (guarded by policy/env)")
    args = ap.parse_args()

    # Import from repo root (script may be launched from launchd with different cwd).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from ui.server import apply_optimizer_suggestions, run_feedback_loop_once

    payload = run_feedback_loop_once(
        edge_bias_days=int(args.edge_bias_days),
        model_bias_days=int(args.model_bias_days),
        place_bias_days=int(args.place_bias_days),
        strategy_days=int(args.strategy_days),
        orphan_fill_days=int(args.orphan_fill_days),
        end_date=str(args.end_date or ""),
    )
    auto_apply = args.apply or (str(os.environ.get("PM_OPTIMIZER_AUTO_APPLY", "") or "").strip().lower() in {"1", "true", "yes", "on"})
    if auto_apply:
        payload["auto_apply"] = apply_optimizer_suggestions(end_date=str(args.end_date or ""))
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
