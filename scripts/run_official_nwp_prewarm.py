#!/usr/bin/env python3
"""Assemble the four official/authorised NWP sources for the AMSC hot path."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_multimodel_prewarm import (  # noqa: E402
    build_official_nwp_prewarm_plan,
    persist_official_nwp_prewarm,
)


SCHEMA = "official_nwp_prewarm_runner_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_multimodel_zuuu_v1" / "prewarm" / "current.json")
    parser.add_argument("--status-path", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_multimodel_zuuu_v1" / "prewarm" / "status_latest.json")
    parser.add_argument("--max-snapshot-age-sec", type=int, default=1200)
    args = parser.parse_args(argv)
    plan = build_official_nwp_prewarm_plan(
        catalog_path=args.catalog.expanduser().resolve(),
        source_root=args.source_root.expanduser().resolve(),
        cache_path=args.cache.expanduser().resolve(),
        max_snapshot_age_sec=max(1, int(args.max_snapshot_age_sec)),
    )
    result = persist_official_nwp_prewarm(plan=plan) if plan.get("ok") is True else {
        "schema": "official_nwp_multimodel_prewarm_result_v1",
        "ok": False,
        "state": "blocked",
        "blockers": list(plan.get("blockers") or []),
        "boundary": plan.get("boundary") or {},
    }
    report = {
        "schema": SCHEMA,
        "attempted_at": _now(),
        "ok": result.get("ok") is True,
        "state": result.get("state"),
        "blockers": list(result.get("blockers") or []),
        "plan": plan,
        "result": result,
        "boundary": {
            "network_acquisition": False,
            "writes_runtime_cache": result.get("ok") is True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "writes_task_queue": False,
            "places_orders": False,
        },
    }
    _atomic_json(args.status_path.expanduser().resolve(), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
