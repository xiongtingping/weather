#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import argparse
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "archive.db"
UI_SERVER = ROOT / "ui" / "server.py"
ENV_PATH = ROOT / ".env"


def _load_dotenv_defaults() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    except sqlite3.Error:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DB))
    parser.add_argument("--immutable", action="store_true", help="open SQLite file as immutable read-only")
    return parser.parse_args()


def main() -> int:
    _load_dotenv_defaults()
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {
        "db": str(db_path),
        "ui_server": str(UI_SERVER),
    }

    if not db_path.exists():
        blockers.append("archive_db_missing")
    else:
        uri = f"file:{db_path}?mode=ro"
        if args.immutable:
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            tables = {}
            for table in (
                "auto_trade_decisions",
                "auto_trade_runs",
                "auto_trade_jobs",
                "event_market_ticks",
                "event_market_tick_rollups",
                "edge_trades",
                "auto_orders",
            ):
                exists = _table_exists(conn, table)
                tables[table] = {
                    "exists": exists,
                    "rows": _count(conn, table) if exists else None,
                }
                if table in {"auto_trade_decisions", "event_market_ticks"} and not exists:
                    blockers.append(f"{table}_missing")
            details["tables"] = tables
            if tables.get("auto_trade_decisions", {}).get("rows") == 0:
                warnings.append("auto_trade_decisions_empty")
            if not tables.get("event_market_tick_rollups", {}).get("exists"):
                warnings.append("event_market_tick_rollups_missing")
            latest_decision = conn.execute(
                """
                SELECT id, run_id, action, decided_state, will_order, reason_codes_json, created_at
                FROM auto_trade_decisions
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone() if tables.get("auto_trade_decisions", {}).get("exists") else None
            details["latest_decision"] = dict(zip(
                ["id", "run_id", "action", "decided_state", "will_order", "reason_codes_json", "created_at"],
                latest_decision,
            )) if latest_decision else None
        finally:
            conn.close()

    if not UI_SERVER.exists():
        blockers.append("ui_server_missing")
    else:
        text = UI_SERVER.read_text(errors="replace")
        details["ui_snapshot_function"] = "def ui_snapshot" in text
        details["ui_snapshot_route"] = 'path == "/api/ui_snapshot"' in text or "path == '/api/ui_snapshot'" in text
        details["auto_trade_criteria_route"] = 'path == "/api/auto-trade-criteria"' in text or "path == '/api/auto-trade-criteria'" in text
        if not details["ui_snapshot_function"] or not details["ui_snapshot_route"]:
            blockers.append("ui_snapshot_endpoint_missing")
        if not details["auto_trade_criteria_route"]:
            warnings.append("auto_trade_criteria_endpoint_missing")

    details["archive_governance_apply_enabled"] = str(
        os.environ.get("PM_ARCHIVE_DB_GOVERNANCE_APPLY") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not details["archive_governance_apply_enabled"]:
        warnings.append("archive_governance_apply_not_enabled")

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
