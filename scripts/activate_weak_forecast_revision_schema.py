#!/usr/bin/env python3
"""Audit or activate the additive live weak-forecast revision schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weak_forecast_revision_schema_contract import audit_weak_forecast_revision_schema, ensure_weak_forecast_revision_schema


CONFIRM = "ACTIVATE_WEAK_FORECAST_REVISION_SCHEMA"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    if args.apply:
        if not args.writer_quiescent or args.confirm != CONFIRM:
            result = {"ok": False, "state": "blocked", "reason": "writer_quiescence_and_confirmation_required"}
        else:
            conn = sqlite3.connect(db)
            try:
                conn.execute("BEGIN IMMEDIATE")
                ensure_weak_forecast_revision_schema(conn)
                conn.commit()
                result = {**audit_weak_forecast_revision_schema(conn), "state": "applied"}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            result = {**audit_weak_forecast_revision_schema(conn), "state": "already_applied_or_missing"}
        finally:
            conn.close()
    result.update({"schema": "activate_weak_forecast_revision_schema_v1", "db": str(db), "places_orders": False})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
