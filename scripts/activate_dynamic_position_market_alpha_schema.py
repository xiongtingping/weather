#!/usr/bin/env python3
"""Controlled schema cutover for position-lot SSOT and market-alpha cycles.

Default mode is read-only.  Apply never enables decision writing, CLOB submit,
or the disabled activation artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_position_lot_schema_contract import (  # noqa: E402
    apply_dynamic_three_leg_position_lot_schema,
    dynamic_three_leg_position_lot_schema_plan,
)
from market_alpha_take_profit_schema_contract import (  # noqa: E402
    apply_market_alpha_take_profit_schema,
    market_alpha_take_profit_schema_plan,
)
from dynamic_three_leg_continuation_schema_contract import (  # noqa: E402
    apply_continuation_schema,
    continuation_schema_plan,
)


SCHEMA_VERSION = "dynamic_position_market_alpha_schema_cutover_v1"
CONFIRMATION = "ACTIVATE_DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA"
MAX_BACKUP_AGE_SECONDS = 900


def _schema_sha256(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("utf-8")).hexdigest()


def _backup_evidence(source: Path, backup: Path | None) -> dict[str, Any]:
    if backup is None:
        return {"ok": False, "block_reasons": ["fresh_backup_required"]}
    target = backup.expanduser().resolve()
    if target == source:
        return {"ok": False, "block_reasons": ["backup_path_must_differ_from_source"]}
    try:
        age = max(0.0, time.time() - target.stat().st_mtime)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as left, sqlite3.connect(
            f"file:{target}?mode=ro", uri=True
        ) as right:
            source_pages = int(left.execute("PRAGMA page_count").fetchone()[0])
            backup_pages = int(right.execute("PRAGMA page_count").fetchone()[0])
            source_page_size = int(left.execute("PRAGMA page_size").fetchone()[0])
            backup_page_size = int(right.execute("PRAGMA page_size").fetchone()[0])
            source_schema = _schema_sha256(left)
            backup_schema = _schema_sha256(right)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "block_reasons": [f"backup_verification_failed:{type(exc).__name__}"],
            "path": str(target),
        }
    blockers = []
    if age > MAX_BACKUP_AGE_SECONDS:
        blockers.append("backup_not_fresh")
    if source_pages != backup_pages or source_page_size != backup_page_size:
        blockers.append("backup_page_geometry_mismatch")
    if source_schema != backup_schema:
        blockers.append("backup_schema_identity_mismatch")
    return {
        "ok": not blockers,
        "block_reasons": blockers,
        "path": str(target),
        "age_seconds": age,
        "source_page_count": source_pages,
        "backup_page_count": backup_pages,
        "source_schema_sha256": source_schema,
        "backup_schema_sha256": backup_schema,
    }


def inspect(db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        lots = dynamic_three_leg_position_lot_schema_plan(conn)
        alpha = market_alpha_take_profit_schema_plan(conn)
        continuation = continuation_schema_plan(conn)
    ready = lots.get("state") == "ready" and alpha.get("state") == "ready" and continuation.get("state") == "ready"
    return {
        "schema": SCHEMA_VERSION,
        "ok": ready,
        "state": "ready" if ready else "apply_required",
        "db": str(db),
        "position_lot_schema": lots,
        "market_alpha_schema": alpha,
        "continuation_schema": continuation,
        "read_only": True,
        "places_orders": False,
        "changes_activation": False,
    }


def apply(db: Path, *, backup: Path | None, writer_quiescent: bool, confirmation: str) -> dict[str, Any]:
    blockers = []
    if not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    if confirmation != CONFIRMATION:
        blockers.append("explicit_confirmation_required")
    backup_result = _backup_evidence(db, backup)
    blockers.extend(backup_result.get("block_reasons") or [])
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "backup": backup_result,
            "places_orders": False,
        }
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN EXCLUSIVE")
        lots = apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
        alpha = apply_market_alpha_take_profit_schema(conn, writer_quiescent=True)
        continuation = apply_continuation_schema(conn, writer_quiescent=True)
        if lots.get("ok") is not True or alpha.get("ok") is not True or continuation.get("ok") is not True:
            raise RuntimeError("dynamic_position_or_market_alpha_schema_apply_failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        **inspect(db),
        "applied": True,
        "backup": backup_result,
        "read_only": False,
        "places_orders": False,
        "changes_activation": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    result = (
        apply(
            args.db.expanduser().resolve(),
            backup=args.backup,
            writer_quiescent=args.writer_quiescent,
            confirmation=args.confirm,
        )
        if args.apply
        else inspect(args.db.expanduser().resolve())
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
