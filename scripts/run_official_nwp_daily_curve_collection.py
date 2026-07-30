#!/usr/bin/env python3
"""Plan or explicitly execute one isolated official NWP daily-curve capture."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_curve_collector_runtime import collect_official_daily_curve  # noqa: E402
from official_nwp_daily_curve_plan_runtime import load_daily_curve_run_contract  # noqa: E402


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("ecmwf", "icon", "gfs", "jma"))
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--target-event-date", required=True)
    parser.add_argument("--execute", action="store_true", help="perform direct official requests; default only emits a no-network plan")
    parser.add_argument("--contract", type=Path, default=ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--curve-source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2")
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--max-point-workers", type=int, default=1)
    args = parser.parse_args(argv)
    result = collect_official_daily_curve(
        contract=load_daily_curve_run_contract(args.contract.expanduser().resolve()),
        provider_id=args.provider,
        run_at_utc=args.run_at_utc,
        target_event_date=args.target_event_date,
        curve_source_root=args.curve_source_root,
        catalog_path=args.catalog,
        timeout_sec=args.timeout_sec,
        max_point_workers=max(1, min(4, args.max_point_workers)),
        execute=args.execute,
    )
    result["attempted_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if args.status_path:
        _atomic_json(args.status_path.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
