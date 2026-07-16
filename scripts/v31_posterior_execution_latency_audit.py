#!/usr/bin/env python3
"""Read the current v3.1 posterior-to-decision timing chain; never writes."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v31_posterior_execution_latency_audit import audit_v31_posterior_execution_latency


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--latest-only", action="store_true")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        print(json.dumps({"ok": False, "state": "blocked", "blockers": ["archive_db_missing"]}))
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = audit_v31_posterior_execution_latency(
            conn,
            limit=max(1, int(args.limit)),
            latest_only=bool(args.latest_only),
        )
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
