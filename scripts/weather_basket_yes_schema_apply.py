#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_basket_yes_schema import apply_weather_basket_yes_schema


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"ok": False, "state": "dry_run", "db": str(args.db)}))
        return 0
    conn = sqlite3.connect(args.db, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        result = apply_weather_basket_yes_schema(conn)
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
