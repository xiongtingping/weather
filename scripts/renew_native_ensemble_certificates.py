#!/usr/bin/env python3
"""Renew every native-ensemble certificate that is close to its end.

User ruling 2026-09-23: 「到期前端预警，不过期」.  The same evidence under a
new thirty-day window, generation advanced, only where nothing fresher
exists for the scope; a receipt per renewal is what the board's warning
reads.  Rules in ``bayes_basket_target/native_ensemble_certificate_clock``;
this is the clock's hand, run daily by ``scripts/calibration_refit_sweep.sh``.

Read-only unless ``--commit``: without it the plan is printed and nothing is
written (the store is opened ``mode=ro``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_ensemble_certificate_clock import (  # noqa: E402
    RENEW_WITHIN_DAYS,
    VALID_DAYS,
    renew_expiring_certificates,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--as-of", default=None,
        help="ISO-8601 UTC instant to run the clock at (default: now)",
    )
    parser.add_argument(
        "--within-days", type=float, default=RENEW_WITHIN_DAYS,
        help="renew a scope whose newest certificate ends inside this",
    )
    parser.add_argument(
        "--valid-days", type=int, default=VALID_DAYS,
        help="the renewed window, in days",
    )
    parser.add_argument(
        "--full-plan", action="store_true",
        help="print every planned item, not only the counts",
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    now = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of else datetime.now(timezone.utc)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0,
        )
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        result = renew_expiring_certificates(
            conn,
            now=now,
            commit=args.commit,
            within_days=args.within_days,
            valid_days=args.valid_days,
        )
    finally:
        conn.close()
    if not args.full_plan:
        plan = result.pop("plan")
        result["plan_items_by_role"] = {
            role: sum(1 for item in plan["items"] if item["role"] == role)
            for role in ("cross", "cross_member", "system")
        }
        result["receipts"] = len(result["receipts"])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
