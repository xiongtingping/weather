#!/usr/bin/env python3
"""Run the isolated official live-snapshot collector; default is no-network."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_curve_monitor_job_runtime import load_monitor_job_contract
from official_nwp_daily_curve_plan_runtime import load_daily_curve_run_contract
from official_nwp_live_snapshot_monitor_runtime import build_official_nwp_live_snapshot_monitor_job


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--job-contract", type=Path, default=ROOT / "config/forecast_sources/official_nwp_live_snapshot_monitor_job_v1.json")
    parser.add_argument("--curve-contract", type=Path, default=ROOT / "config/forecast_sources/zuuu_official_daily_curve_run_contract_v1.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / "data/runtime/official_nwp_sources")
    parser.add_argument("--prewarm-cache", type=Path, default=ROOT / "data/runtime/official_nwp_multimodel_zuuu_v1/prewarm/current.json")
    args = parser.parse_args()
    job = load_monitor_job_contract(args.job_contract)
    execute = bool(args.execute and _truthy(os.environ.get(str(job.get("enable_environment_key") or ""))))
    result = build_official_nwp_live_snapshot_monitor_job(job_contract=job, curve_contract=load_daily_curve_run_contract(args.curve_contract), now_utc=None, source_root=args.source_root.resolve(), catalog_path=args.catalog.resolve(), prewarm_cache=args.prewarm_cache.resolve(), execute=execute)
    if args.execute and not execute:
        result.update({"ok": False, "state": "blocked", "blockers": ["official_nwp_live_snapshot_execute_not_explicitly_enabled"]})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
