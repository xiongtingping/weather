#!/usr/bin/env python3
"""Apply pending AMSC canonical events to the production probability SSOT."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from production_reported_x_canonical_event_drain_runtime import (  # noqa: E402
    drain_reported_x_canonical_events,
    pending_reported_x_canonical_event_ids,
)
from production_probability_successor_activation_contract import (  # noqa: E402
    PRODUCTION_PATH as DEFAULT_ACTIVATION,
    load_successor_activation,
)
from production_probability_p0_runtime import DEFAULT_RESOLUTION_AUTHORITY  # noqa: E402


CONFIRMATION = "APPLY_PRODUCTION_CANONICAL_PROBABILITY_UPDATES"


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--activation", default=str(DEFAULT_ACTIVATION))
    parser.add_argument("--resolution-authority", default=str(DEFAULT_RESOLUTION_AUTHORITY))
    parser.add_argument("--event-date", default=_today())
    parser.add_argument("--public-slug", default="")
    parser.add_argument("--max-events", type=int, default=64)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    event_date = str(args.event_date or "")[:10]
    if not args.apply:
        try:
            activation = load_successor_activation(args.activation)
            conn = sqlite3.connect(f"file:{Path(args.db).expanduser().resolve()}?mode=ro", uri=True)
            try:
                pending = pending_reported_x_canonical_event_ids(
                    conn, event_date=event_date, activation=activation
                )
            finally:
                conn.close()
            result = {
                "schema": "production_reported_x_canonical_event_drain_v1",
                "ok": True,
                "state": "read_only_plan",
                "event_date": event_date,
                "pending_event_ids": pending,
                "places_orders": False,
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            result = {"schema": "production_reported_x_canonical_event_drain_v1", "ok": False, "state": "blocked", "reason": f"read_only_plan_failed:{exc}", "places_orders": False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") is True else 1
    if args.confirm != CONFIRMATION or event_date != _today():
        result = {
            "schema": "production_reported_x_canonical_event_drain_v1",
            "ok": False,
            "state": "blocked",
            "reason": "confirmation_required_or_noncurrent_event_date",
            "places_orders": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        activation = load_successor_activation(args.activation)
        with connect_archive_db_rw(Path(args.db), owner="production_canonical_probability_updates") as conn:
            result = drain_reported_x_canonical_events(
                conn,
                activation=activation,
                event_date=event_date,
                public_slug=args.public_slug,
                resolution_authority_path=args.resolution_authority,
                max_events=args.max_events,
            )
    except Exception as exc:
        result = {"schema": "production_reported_x_canonical_event_drain_v1", "ok": False, "state": "blocked", "reason": f"apply_failed:{type(exc).__name__}:{exc}", "places_orders": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
