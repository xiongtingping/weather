#!/usr/bin/env python3
"""Renew every served delta-v2 fill batch that is close to its end.

The same distributions under a new year of validity, so production's numbers
do not move and the policies' batch pins keep binding.  The backstop behind
the weekly refit: if the shadow book stops or the fit keeps failing, the
batch production executes against still never simply ends -- the board's
staleness lamp is what says the numbers have aged.

Read-only unless ``--commit``.
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

from bayes_basket_target.delta_v2_authority_clock import (  # noqa: E402
    RENEW_WITHIN_DAYS,
    renew_expiring_fill_batches,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--within-days", type=float, default=RENEW_WITHIN_DAYS,
        help="renew a served batch that ends inside this many days",
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")
    receipt = renew_expiring_fill_batches(
        conn,
        now=datetime.now(timezone.utc),
        commit=args.commit,
        within_days=args.within_days,
    )
    conn.close()
    print(json.dumps(
        {"schema": "delta_v2_fill_renewal_v1", "committed": args.commit,
         **receipt},
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
