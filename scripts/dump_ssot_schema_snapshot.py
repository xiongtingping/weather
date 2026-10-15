#!/usr/bin/env python3
"""Write the live SSOT's DDL and table sizes for the query-plan guard.

``tests/test_sql_query_plans.py`` plans every SQL statement in the code
against this snapshot: the code's own schema module builds 78 of the live
163 tables, and the plans that matter are the live indexes'.  Re-run after a
migration that adds or drops an index, and commit the file.  Read-only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "ssot_schema_snapshot.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    tables = {}
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
        " AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        # max(rowid) is an index seek; COUNT(*) on the big tables is minutes.
        try:
            rows = conn.execute(f'SELECT max(rowid) FROM "{name}"').fetchone()[0] or 0
        except sqlite3.Error:
            rows = 0
        tables[name] = {"sql": sql, "rows": int(rows)}
    indexes = [
        sql for (sql,) in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            " ORDER BY name"
        )
    ]
    args.out.write_text(
        json.dumps({"tables": tables, "indexes": indexes}, indent=1, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    print(f"{len(tables)} tables, {len(indexes)} indexes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
