#!/usr/bin/env python3
"""Retire registry row copies no reader queries, bounded, newest kept.

See ``bayes_basket_target.market_registry_retention`` for why this exists
and what it is not allowed to touch.  This is the runner: it opens the one
writer, plans outside a transaction, and opens one only when the plan has
something in it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry_retention import (  # noqa: E402
    apply_market_registry_retention,
    plan_market_registry_retention,
)


def main() -> int:
    database = Path(DEFAULT_TARGET_DB).expanduser().resolve()
    conn = sqlite3.connect(database, timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        plan = plan_market_registry_retention(conn)
        if not plan["stale_snapshots"]:
            print(json.dumps({**plan, "state": "nothing_stale"}, indent=1))
            return 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            receipt = apply_market_registry_retention(conn, plan)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        receipt.pop("stale_snapshots", None)
        print(json.dumps(receipt, indent=1))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
