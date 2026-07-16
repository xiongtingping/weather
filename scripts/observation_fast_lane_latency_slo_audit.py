#!/usr/bin/env python3
"""Read an immutable fast-lane packet collection and emit a no-write SLO report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observation_fast_lane_latency_runtime import build_observation_fast_lane_latency_slo_report


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if isinstance(value, dict) and isinstance(value.get("records"), list):
        return [dict(row) for row in value["records"] if isinstance(row, dict)]
    raise ValueError("fast_lane_latency_input_requires_json_array_or_records_array")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only observation fast-lane SLO audit")
    parser.add_argument("--input", required=True, help="JSON array or {records:[...]} packet file")
    parser.add_argument("--require-live-t1-to-t5", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        report = build_observation_fast_lane_latency_slo_report(
            _records(payload),
            require_live_t1_to_t5=bool(args.require_live_t1_to_t5),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "state": "blocked", "error": str(exc), "read_only": True}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
