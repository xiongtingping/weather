#!/usr/bin/env python3
"""Emit a read-only settled-attribution receipt for the target SSOT."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.settled_attribution import MAX_FACTS, build_settled_attribution
from bayes_basket_target.database import resolve_target_database


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only DeltaBasketScore settled attribution audit"
    )
    parser.add_argument("--database", required=True, help="target SQLite SSOT")
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--fact-id")
    parser.add_argument("--max-facts", type=int, default=MAX_FACTS)
    parser.add_argument("--after-fact-rowid", type=int, default=0)
    args = parser.parse_args()
    candidate = Path(args.database).expanduser()
    if candidate.is_symlink():
        parser.error("--database symlink is forbidden")
    database = resolve_target_database(candidate)
    if not database.is_file():
        parser.error("--database must name an existing SQLite file")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        receipt = build_settled_attribution(
            connection,
            as_of_utc=args.as_of_utc,
            fact_id=args.fact_id,
            max_facts=args.max_facts,
            after_fact_rowid=args.after_fact_rowid,
        )
    finally:
        connection.close()
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
