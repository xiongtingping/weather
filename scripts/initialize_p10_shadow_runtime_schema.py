#!/usr/bin/env python3
"""Initialize empty P10 tables only in an explicitly isolated shadow DB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_shadow_runtime_schema import (  # noqa: E402
    build_p10_shadow_runtime_schema_plan,
    initialize_p10_shadow_runtime_schema,
)
from sequential_bayes_public_ssot_shadow_writer import (  # noqa: E402
    SHADOW_MANIFEST_TABLE,
    initialize_isolated_public_ssot_shadow_schema,
)


CONFIRMATION = "INITIALIZE_P10_ISOLATED_SHADOW_RUNTIME"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _bootstrap_empty_p10_db(conn: sqlite3.Connection) -> dict:
    """Create the isolation sentinel only for an otherwise empty P10 DB.

    P2 is an evidence mirror and may already have canonical rows.  It must
    never be upgraded in place into the P10 runtime, because doing so would
    merge legacy first-seen timing with direct-sidecar timing.
    """

    if _table_exists(conn, "canonical_observation_events"):
        count = int(conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] or 0)
        if count:
            raise ValueError("p10_runtime_db_contains_preexisting_canonical_events")
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        non_system_tables = [
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        ]
        if non_system_tables:
            raise ValueError("p10_runtime_db_uninitialized_but_not_empty")
        initialize_isolated_public_ssot_shadow_schema(conn)
        return {"bootstrapped_isolated_manifest": True}
    return {"bootstrapped_isolated_manifest": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", default=str(ROOT / "data" / "shadow" / "p10_runtime.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    path = Path(args.shadow_db).expanduser().resolve()
    if path.name == "archive.db":
        print(json.dumps({"ok": False, "state": "blocked", "blockers": ["production_archive_db_forbidden"]}))
        return 2
    if args.apply and args.confirm != CONFIRMATION:
        print(json.dumps({"ok": False, "state": "blocked", "blockers": ["p10_shadow_runtime_schema_confirmation_invalid"]}))
        return 2
    try:
        if args.apply:
            with sqlite3.connect(path, timeout=30) as conn:
                bootstrap = _bootstrap_empty_p10_db(conn)
                result = initialize_p10_shadow_runtime_schema(conn)
                result["bootstrap"] = bootstrap
        else:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                result = build_p10_shadow_runtime_schema_plan(conn)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {"ok": False, "state": "blocked", "blockers": [f"p10_shadow_runtime_schema_failed:{type(exc).__name__}"]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
