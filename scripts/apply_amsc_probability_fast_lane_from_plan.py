#!/usr/bin/env python3
"""Apply the AMSC SourceObservation -> canonical -> posterior SSOT fast lane.

The fetch plan and raw archive are already immutable before this command
starts.  This command deliberately does not materialize a legacy fact,
enqueue an old posterior task, fetch weather/model data, or place orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_reported_x_amsc_archive_probability_ingress import (  # noqa: E402
    apply_reported_x_amsc_archive_probability_ingress,
)


SCHEMA_VERSION = "amsc_probability_fast_lane_from_plan_v1"
CONFIRMATION = "APPLY_AMSC_PROBABILITY_FAST_LANE"


def _now_iso() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--raw-archive-dir", type=Path, default=ROOT / "data" / "amsc_raw_history")
    parser.add_argument("--activation", type=Path, default=ROOT / "config" / "production_probability_runtime_activation.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-queue-wait-sec", type=float, default=0.75)
    parser.add_argument("--db-timeout-sec", type=float, default=0.75)
    parser.add_argument("--db-busy-timeout-ms", type=int, default=500)
    args = parser.parse_args(argv)

    blockers: list[str] = []
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        plan = {}
        blockers.append(f"plan_unreadable:{type(exc).__name__}")
    payload = _mapping(plan.get("effective_payload"))
    event_date = str(plan.get("event_date") or payload.get("event_date") or "")[:10]
    observations = list(payload.get("source_observations") or [])
    if args.apply is not True or args.confirm != CONFIRMATION:
        blockers.append("apply_confirmation_missing_or_invalid")
    if event_date != _today():
        blockers.append("noncurrent_physical_station_day_forbidden")
    if not observations:
        blockers.append("source_observations_missing")
    if not any(
        isinstance(row, Mapping)
        and str(row.get("source_id") or "") == "zuuu_metar_raw_amsc"
        and str(row.get("event_date") or "")[:10] == event_date
        for row in observations
    ):
        blockers.append("primary_amsc_observation_missing")
    if blockers:
        print(json.dumps({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": blockers}, sort_keys=True))
        return 2

    started_at = _now_iso()
    started = time.monotonic()
    try:
        result = apply_reported_x_amsc_archive_probability_ingress(
            db_path=args.db,
            raw_archive_dir=args.raw_archive_dir,
            event_date=event_date,
            decision_as_of=started_at,
            activation_path=args.activation,
            source_observation_payload=payload,
            source_observation_created_at=started_at,
            writer_queue_wait_sec=max(0.0, float(args.writer_queue_wait_sec)),
            db_timeout_sec=max(0.0, float(args.db_timeout_sec)),
            db_busy_timeout_ms=max(0, int(args.db_busy_timeout_ms)),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "fast_lane_failed_evidence_retained_in_raw_archive",
            "blockers": [f"fast_lane_exception:{type(exc).__name__}:{exc}"],
            "places_orders": False,
        }
    result = dict(result)
    result["fast_lane"] = {
        "schema": SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "legacy_fact_projection_precedes_probability": False,
        "raw_archive_already_durable": True,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
