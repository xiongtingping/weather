#!/usr/bin/env python3
"""Append the truth lane's settlement label for every standing revision.

Charter 140: the lambda, beta and valuation lanes read their labels from
``basket_station_day_settlement_label``.  Without ``--commit`` it resolves
the labels read-only and prints the counts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.candidate_window_evidence import (  # noqa: E402
    LABEL_FLOOR_EVENT_DATE,
    refresh_settlement_labels,
    standing_labels,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--floor", default=LABEL_FLOOR_EVENT_DATE)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=120.0)
        conn.execute("PRAGMA busy_timeout=120000")
        try:
            print(json.dumps(refresh_settlement_labels(
                conn, floor_event_date=args.floor,
            )))
        finally:
            conn.close()
        return 0
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=120.0)
    try:
        _rows, counts = standing_labels(
            conn, floor_event_date=args.floor, only_unlabelled=False,
        )
        print(json.dumps(counts))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
