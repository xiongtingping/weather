#!/usr/bin/env python3
"""Run the low-frequency prospective curve job; default is no-network planning."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_curve_monitor_job_runtime import (  # noqa: E402
    build_official_nwp_daily_curve_monitor_job,
    load_monitor_job_contract,
)
from official_nwp_daily_curve_plan_runtime import load_daily_curve_run_contract  # noqa: E402


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_completed_provider_runs(path: Path) -> dict[str, str]:
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(previous, dict):
        return {}
    direct = previous.get("completed_provider_runs")
    if isinstance(direct, dict):
        return {str(provider): str(run) for provider, run in direct.items() if str(provider) and str(run)}
    completed: dict[str, str] = {}
    for row in previous.get("provider_results") or []:
        if not isinstance(row, dict) or row.get("ok") is not True or row.get("state") not in {"collected", "already_collected"}:
            continue
        provider, run = str(row.get("provider_id") or ""), str(row.get("run_at_utc") or "")
        if provider and run:
            completed[provider] = run
    return completed


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _try_acquire_run_lease(path: Path):
    """Acquire a process-lifetime exclusive lease for one curve monitor run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid()}) + "\n")
    handle.flush()
    return handle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--job-contract", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_daily_curve_monitor_job_v1.json")
    parser.add_argument("--curve-contract", type=Path, default=ROOT / "config" / "forecast_sources" / "zuuu_official_daily_curve_run_contract_v1.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--curve-source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2")
    parser.add_argument("--status-path", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2" / "monitor_job_status_latest.json")
    args = parser.parse_args(argv)
    job = load_monitor_job_contract(args.job_contract)
    allow_execute = args.execute and _truthy(os.environ.get(str(job.get("enable_environment_key") or "PM_OFFICIAL_NWP_CURVE_COLLECTION_ENABLED")))
    status_path = args.status_path.expanduser().resolve()
    lease = _try_acquire_run_lease(status_path.with_suffix(status_path.suffix + ".lock")) if allow_execute else None
    if allow_execute and lease is None:
        result = {
            "schema": "official_nwp_daily_curve_monitor_job_result_v1",
            "ok": False,
            "state": "blocked",
            "blockers": ["official_nwp_curve_monitor_run_already_in_progress"],
            "boundary": {"writes_archive_db": False, "writes_posterior": False, "places_orders": False},
        }
    else:
        try:
            result = build_official_nwp_daily_curve_monitor_job(job_contract=job, curve_contract=load_daily_curve_run_contract(args.curve_contract), execute=allow_execute, curve_source_root=args.curve_source_root, catalog_path=args.catalog, completed_provider_runs=_load_completed_provider_runs(status_path))
        finally:
            if lease is not None:
                lease.close()
    if args.execute and not allow_execute:
        result["ok"] = False
        result["state"] = "blocked"
        result["blockers"] = sorted(set([*list(result.get("blockers") or []), "official_nwp_curve_monitor_execute_not_explicitly_enabled"]))
    result["status_path"] = str(status_path)
    if allow_execute and "official_nwp_curve_monitor_run_already_in_progress" not in list(result.get("blockers") or []):
        _atomic_json(status_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
