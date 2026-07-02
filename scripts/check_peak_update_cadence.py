#!/usr/bin/env python3
"""Read-only verifier for D0 peak-likelihood refresh cadence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / ".venv" / "bin" / "python"
if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

from scan_weather_market import _temperature_peak_likelihood_refresh_interval_sec  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _zone(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def _sample_date(day_offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=int(day_offset))).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--peak-hour", type=float, default=16.0)
    args = ap.parse_args()

    tz = _zone(str(args.tz or "America/New_York"))
    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    # (hours before peak, required max minutes, label)
    before_cases = [
        (5.0, 15.0, "gt_4h"),
        (3.0, 10.0, "2_to_4h"),
        (1.0, 5.0, "0_to_2h"),
        (0.0, 2.0, "peak_window"),
    ]
    after_cases = [
        (0.5, 2.0, "post_peak_window"),
    ]
    for day in range(max(1, int(args.days))):
        event_date = _sample_date(day)
        base = datetime.fromisoformat(event_date).replace(tzinfo=tz)
        for hours_before, max_min, label in before_cases:
            local_dt = base.replace(
                hour=int(args.peak_hour),
                minute=int((float(args.peak_hour) % 1.0) * 60),
                second=0,
                microsecond=0,
            ) - timedelta(hours=float(hours_before))
            interval = _temperature_peak_likelihood_refresh_interval_sec(
                event_date,
                str(args.tz),
                "temperature_2m_max",
                now_dt=local_dt.astimezone(timezone.utc),
            )
            got_min = None if interval is None else float(interval) / 60.0
            row = {
                "event_date": event_date,
                "case": label,
                "local_time": local_dt.isoformat(),
                "interval_min": got_min,
                "required_max_min": float(max_min),
            }
            checks.append(row)
            if got_min is None or got_min > float(max_min) + 1e-9:
                violations.append(row)
        for hours_after, max_min, label in after_cases:
            local_dt = base.replace(
                hour=int(args.peak_hour),
                minute=int((float(args.peak_hour) % 1.0) * 60),
                second=0,
                microsecond=0,
            ) + timedelta(hours=float(hours_after))
            interval = _temperature_peak_likelihood_refresh_interval_sec(
                event_date,
                str(args.tz),
                "temperature_2m_max",
                now_dt=local_dt.astimezone(timezone.utc),
            )
            got_min = None if interval is None else float(interval) / 60.0
            row = {
                "event_date": event_date,
                "case": label,
                "local_time": local_dt.isoformat(),
                "interval_min": got_min,
                "required_max_min": float(max_min),
            }
            checks.append(row)
            if got_min is None or got_min > float(max_min) + 1e-9:
                violations.append(row)

    report = {
        "ok": not violations,
        "days": max(1, int(args.days)),
        "tz": str(args.tz),
        "checked": len(checks),
        "violations": violations,
        "checks": checks[:40],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
