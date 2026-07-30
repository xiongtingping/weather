#!/usr/bin/env python3
"""Prepare one plan-only basket + reported-trajectory-NO decision.

The input is an already materialized weather/market snapshot JSON object.  No
network, database, market refresh, or order gateway is used.  The output is a
JSON plan for acceptance/audit; this script never submits it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

# Keep this standalone script runnable from the repository root without
# installing the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basket_strategy import build_plan_from_weather_snapshot


def _read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("snapshot root must be a JSON object")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a plan-only basket + hard-NO JSON artifact")
    parser.add_argument("--input", required=True, type=Path, help="materialized weather/market snapshot JSON")
    parser.add_argument("--output", type=Path, help="optional output JSON path; otherwise print to stdout")
    args = parser.parse_args()

    plan = build_plan_from_weather_snapshot(_read_json(args.input))
    if plan.get("live_submit_authorized") is not False:
        raise RuntimeError("live submission authorization must remain false")
    rendered = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
