#!/usr/bin/env python3
"""Apply immutable AMSC plan through the unique production v6 ingress."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_v6_amsc_archive_ingress import apply_v6_amsc_archive_probability_ingress


SCHEMA_VERSION = "amsc_probability_fast_lane_v6_from_plan_v1"
CONFIRMATION = "APPLY_AMSC_PROBABILITY_FAST_LANE_V6"
ZUUU_EVENT_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def current_zuuu_event_date(now: datetime | None = None) -> str:
    """Return the physical station day, never the Polymarket ET run date."""

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZUUU_EVENT_TIME_ZONE).date().isoformat()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--raw-archive-dir", type=Path, default=ROOT / "data/amsc_raw_history")
    parser.add_argument("--activation", type=Path, default=ROOT / "config/production_probability_runtime_activation.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-queue-wait-sec", type=float, default=0.75)
    parser.add_argument("--db-timeout-sec", type=float, default=0.75)
    parser.add_argument("--db-busy-timeout-ms", type=int, default=500)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": f"plan_unreadable:{exc}"}))
        return 2
    payload = _mapping(plan.get("effective_payload"))
    day = _text(plan.get("event_date") or payload.get("event_date"))[:10]
    today = current_zuuu_event_date()
    if args.apply is not True or args.confirm != CONFIRMATION or day != today:
        print(json.dumps({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "confirmation_or_day_invalid"}))
        return 2
    started = time.monotonic()
    instant = _now()
    try:
        result = apply_v6_amsc_archive_probability_ingress(
            db_path=args.db,
            raw_archive_dir=args.raw_archive_dir,
            event_date=day,
            decision_as_of=instant,
            activation_path=args.activation,
            source_observation_payload=payload,
            source_observation_created_at=instant,
            writer_queue_wait_sec=max(0.0, args.writer_queue_wait_sec),
            db_timeout_sec=max(0.0, args.db_timeout_sec),
            db_busy_timeout_ms=max(0, args.db_busy_timeout_ms),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "v6_fast_lane_failed_raw_evidence_retained",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
        }
    result = dict(result)
    result["fast_lane_elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


def _text(value: Any) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
