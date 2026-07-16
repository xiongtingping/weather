#!/usr/bin/env python3
"""Plan or explicitly apply the additive v6 production-successor schema."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.schema_contract import build_forward_plan, verify_schema


CONFIRM = "ACTIVATE_PHYSICAL_STATE_BAYES_V6_SCHEMA"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    plan = build_forward_plan(db)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0 if plan.get("ok") else 2
    if not args.writer_quiescent or args.confirm != CONFIRM:
        raise SystemExit("apply requires --writer-quiescent and exact --confirm token")
    if not plan.get("ok"):
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 2
    with sqlite3.connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for action in plan.get("actions") or []:
                conn.execute(str(action["sql"]))
            verified = verify_schema(conn)
            if not verified.get("ok"):
                raise RuntimeError("v6 schema postcondition failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print(json.dumps({"ok": True, "state": "applied", "db": str(db)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
