#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
TEMP_PATH_BLOCK_REASON = "temperature_path_trade_follow_not_allowed"


def _safe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return None


def _reason_list(value: Any) -> list[str]:
    parsed = _safe_json(value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item or "").strip()]
    return []


def _traceability_blockers(row: Mapping[str, Any]) -> list[str]:
    details = _safe_json(row.get("details_json"))
    details = details if isinstance(details, Mapping) else {}
    reason_codes = set(_reason_list(row.get("reason_codes_json")))
    blockers: list[str] = []
    posterior_block = str(details.get("posterior_settlement_entry_block_reason") or "").strip()
    temp_block = bool(
        posterior_block == TEMP_PATH_BLOCK_REASON
        or TEMP_PATH_BLOCK_REASON in reason_codes
        or details.get("temperature_path_trade_follow_block_yes") is True
        or details.get("temperature_path_trade_follow_allowed") is False
    )
    if not temp_block:
        return blockers
    if details.get("posterior_settlement_entry_allowed") is not False:
        blockers.append("posterior_settlement_entry_allowed_not_false")
    if posterior_block != TEMP_PATH_BLOCK_REASON:
        blockers.append("posterior_settlement_entry_block_reason_missing")
    if details.get("temperature_path_trade_follow_allowed") is not False:
        blockers.append("temperature_path_trade_follow_allowed_missing")
    if details.get("temperature_path_trade_follow_block_yes") is not True:
        blockers.append("temperature_path_trade_follow_block_yes_missing")
    if not str(details.get("temperature_path_trade_follow_reason") or "").strip():
        blockers.append("temperature_path_trade_follow_reason_missing")
    if not str(details.get("temperature_path_trade_follow_gate_source") or "").strip():
        blockers.append("temperature_path_trade_follow_gate_source_missing")
    if details.get("target_realization_trade_follow_allowed") is not False:
        blockers.append("target_realization_trade_follow_allowed_missing")
    if not str(details.get("target_realization_trade_follow_reason") or "").strip():
        blockers.append("target_realization_trade_follow_reason_missing")
    return blockers


def check_db(db_path: Path, *, limit: int = 300) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "ok": False,
            "db": str(db_path),
            "blockers": ["db_missing"],
            "checked_rows": 0,
            "temperature_path_block_rows": 0,
            "items": [],
        }
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """
            SELECT id, run_date, slug, event_date, outcome, side, action,
                   decided_state, will_order, reason_codes_json, details_json, created_at
            FROM auto_trade_decisions
            WHERE action = 'entry'
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    items: list[dict[str, Any]] = []
    blockers: list[str] = []
    temp_rows = 0
    for raw in rows:
        row = dict(raw)
        row_blockers = _traceability_blockers(row)
        details = _safe_json(row.get("details_json"))
        details = details if isinstance(details, Mapping) else {}
        is_temp_row = bool(
            row_blockers
            or str(details.get("posterior_settlement_entry_block_reason") or "").strip()
            == TEMP_PATH_BLOCK_REASON
            or details.get("temperature_path_trade_follow_block_yes") is True
            or details.get("temperature_path_trade_follow_allowed") is False
        )
        if not is_temp_row:
            continue
        temp_rows += 1
        item = {
            "id": row.get("id"),
            "slug": row.get("slug"),
            "event_date": row.get("event_date"),
            "outcome": row.get("outcome"),
            "side": row.get("side"),
            "created_at": row.get("created_at"),
            "ok": not row_blockers,
            "blockers": row_blockers,
            "reason": details.get("temperature_path_trade_follow_reason"),
        }
        items.append(item)
        for blocker in row_blockers:
            blockers.append(f"decision_{row.get('id')}:{blocker}")
    warnings = [] if temp_rows else ["no_temperature_path_trade_follow_decision_rows_in_window"]
    return {
        "ok": not blockers,
        "db": str(db_path),
        "checked_rows": len(rows),
        "temperature_path_block_rows": temp_rows,
        "blockers": blockers,
        "warnings": warnings,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit for temperature path trade-follow traceability in auto_trade_decisions."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args()
    result = check_db(Path(args.db), limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
