#!/usr/bin/env python3
"""Apply a newly archived AMSC TAF revision to the production weak lane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw
from production_probability_successor_activation_contract import load_successor_activation
from production_reported_x_weak_revision_event_runtime import apply_reported_x_taf_revision_from_source_observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--activation",
        default=str(ROOT / "config" / "production_probability_runtime_activation.json"),
    )
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        effective = plan.get("effective_payload") if isinstance(plan.get("effective_payload"), dict) else {}
        day = str(plan.get("event_date") or effective.get("event_date") or "")[:10]
        fact_as_of = str(plan.get("fact_as_of") or effective.get("fact_as_of") or "")
        slug = str(plan.get("slug") or effective.get("slug") or "")
        with connect_archive_db_rw(Path(args.db), owner="amsc_taf_forecast_revision") as conn:
            result = apply_reported_x_taf_revision_from_source_observations(
                conn,
                activation=load_successor_activation(args.activation),
                event_date=day,
                source_observations=effective.get("source_observations") or [],
                decision_as_of=fact_as_of,
                public_slug=slug,
            )
    except Exception as exc:
        result = {
            "schema": "apply_amsc_taf_forecast_revision_from_plan_v1",
            "ok": False,
            "state": "blocked",
            "reason": f"taf_forecast_revision_apply_failed:{type(exc).__name__}",
            "exception_detail": str(exc)[:300],
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
