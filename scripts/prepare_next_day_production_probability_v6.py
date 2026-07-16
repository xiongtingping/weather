#!/usr/bin/env python3
"""Prepare next local day's production v6 P0 outside the observation hot path."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_canonical_event_drain_runtime import ensure_v6_daily_p0


SCHEMA_VERSION = "prepare_next_day_production_probability_v6_v1"
CONFIRMATION = "PREPARE_NEXT_DAY_PRODUCTION_PHYSICAL_STATE_BAYES_V6"


def main() -> int:
    parser = argparse.ArgumentParser()
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--activation", type=Path, default=ROOT / "config/production_probability_runtime_activation.json")
    parser.add_argument("--event-date", default=tomorrow)
    parser.add_argument("--curve-root", type=Path, default=DEFAULT_CURVE_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    expected = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
    if not args.apply or args.confirm != CONFIRMATION or args.event_date[:10] != expected:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "confirmation_or_next_day_invalid",
            "places_orders": False,
        }
    else:
        decision_as_of = datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            activation = validate_v6_activation(json.loads(args.activation.read_text(encoding="utf-8")))
            with connect_archive_db_rw(args.db, owner="physical_state_bayes_v6_next_day_p0") as conn:
                p0 = ensure_v6_daily_p0(
                    conn,
                    activation=activation,
                    event_date=expected,
                    decision_as_of=decision_as_of,
                    curve_root=args.curve_root,
                )
            result = {
                "schema": SCHEMA_VERSION,
                "ok": p0.get("ok") is True,
                "state": "next_day_production_p0_ready" if p0.get("ok") is True else "blocked",
                "event_date": expected,
                "daily_p0": p0,
                "hot_path_p0_build_required": False if p0.get("ok") is True else True,
                "strict_asof_oos_live_gate": False,
                "places_orders": False,
            }
        except Exception as exc:
            result = {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": f"next_day_v6_p0_prepare_failed:{type(exc).__name__}",
                "exception_detail": str(exc)[:500],
                "places_orders": False,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
