#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr10_model_oos_basis import build_pr10_model_oos_terminal_basis
from bayes_basket_target.resource_bounds import write_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--expected-basis", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{resolve_target_database(args.db)}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        result = build_pr10_model_oos_terminal_basis(
            conn, expected_basis_path=args.expected_basis, as_of_utc=args.as_of_utc
        )
    finally:
        conn.close()
    write_bounded_json(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "state", "expected_scope_count", "oos_ready_scope_count",
        "blocked_scope_count", "blocker_counts", "content_sha256"
    )}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
