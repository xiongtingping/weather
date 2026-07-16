#!/usr/bin/env python3
"""Read-only health audit for the latest hourly forward-capture report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_source_forward_capture_health_runtime import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_REQUIRED_SOURCE_IDS,
    build_forecast_source_forward_capture_health_audit,
)


DEFAULT_CAPTURE_REPORT = ROOT / "data" / "research" / "forecast_source_skill" / "forward" / "hourly_capture_latest.json"


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("forward_capture_latest_report_not_mapping")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", type=Path, default=DEFAULT_CAPTURE_REPORT)
    parser.add_argument("--event-date", default=_event_date())
    parser.add_argument("--now", default=_now_iso())
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    parser.add_argument("--required-source", action="append", default=list(DEFAULT_REQUIRED_SOURCE_IDS))
    args = parser.parse_args()
    try:
        report = build_forecast_source_forward_capture_health_audit(
            _load(args.capture_report),
            now=args.now,
            expected_event_date=args.event_date,
            required_source_ids=args.required_source,
            max_age_seconds=args.max_age_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {
            "schema": "forecast_source_forward_capture_health_audit_v1",
            "ok": False,
            "state": "blocked",
            "blockers": [f"forward_capture_report_unreadable:{type(exc).__name__}"],
            "read_only": True,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
