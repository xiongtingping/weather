#!/usr/bin/env python3
"""Create or update an isolated P2 canonical shadow mirror.

This command is deliberately separate from all production posterior and order
workers. Its only permitted destination is a SQLite DB with the explicit
``isolated_shadow_only`` sentinel; the production archive is always read-only.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p2_canonical_shadow_mirror import mirror_verified_p2_canonical_events_to_isolated_shadow  # noqa: E402
from sequential_bayes_public_ssot_shadow_writer import (  # noqa: E402
    initialize_isolated_public_ssot_shadow_schema,
)
from sequential_bayes_schema_contract import apply_forward_schema, verify_schema  # noqa: E402


DEFAULT_SOURCE_DB = ROOT / "data" / "archive.db"
DEFAULT_SHADOW_DB = ROOT / "data" / "shadow" / "p2_canonical_amsc_shadow.db"
CONFIRMATION = "INITIALIZE_AND_MIRROR_P2_ISOLATED_SHADOW"


def _today_cst() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _shadow_status(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "state": "missing"}
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            schema = verify_schema(conn)
            manifest = conn.execute(
                "SELECT activation_state FROM v2_shadow_runtime_manifest WHERE singleton=1"
            ).fetchone()
            return {
                "exists": True,
                "state": "ready" if schema.get("ok") and manifest and manifest[0] == "isolated_shadow_only" else "invalid",
                "p1_schema": schema,
                "manifest_state": manifest[0] if manifest else "",
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"exists": True, "state": "unreadable", "error": type(exc).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    parser.add_argument("--shadow-db", default=str(DEFAULT_SHADOW_DB))
    parser.add_argument("--raw-archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument("--event-date", default=_today_cst())
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--initialize-isolated-shadow", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    source = Path(args.source_db).expanduser().resolve()
    shadow = Path(args.shadow_db).expanduser().resolve()
    if source == shadow:
        result = {"ok": False, "state": "blocked", "blockers": ["p2_shadow_mirror_production_archive_destination_forbidden"]}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    if not args.apply:
        result = {
            "schema": "p2_canonical_shadow_mirror_command_v1",
            "ok": True,
            "state": "planned",
            "source_db": str(source),
            "source_db_open_mode": "read_only",
            "shadow_db": str(shadow),
            "shadow_status": _shadow_status(shadow),
            "event_date": str(args.event_date)[:10],
            "writes_production_archive": False,
            "places_orders": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    blockers: list[str] = []
    if args.confirm != CONFIRMATION:
        blockers.append("p2_shadow_mirror_confirmation_missing_or_invalid")
    if not shadow.exists() and not args.initialize_isolated_shadow:
        blockers.append("p2_shadow_mirror_destination_missing_initialize_flag_required")
    if blockers:
        print(json.dumps({"ok": False, "state": "blocked", "blockers": blockers}, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        if args.initialize_isolated_shadow:
            shadow.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        shadow_conn = sqlite3.connect(shadow)
        try:
            if args.initialize_isolated_shadow:
                initialize_isolated_public_ssot_shadow_schema(shadow_conn)
                apply_forward_schema(shadow_conn)
                shadow_conn.commit()
            result = mirror_verified_p2_canonical_events_to_isolated_shadow(
                source_conn,
                shadow_conn,
                event_date=args.event_date,
                station_id=args.station_id,
                raw_archive_dir=args.raw_archive_dir,
            )
        finally:
            source_conn.close()
            shadow_conn.close()
    except Exception as exc:
        result = {
            "schema": "p2_canonical_shadow_mirror_command_v1",
            "ok": False,
            "state": "blocked",
            "blockers": [f"p2_shadow_mirror_apply_failed:{type(exc).__name__}"],
            "writes_production_archive": False,
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
