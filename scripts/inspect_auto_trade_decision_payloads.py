#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
ENV_PATH = ROOT / ".env"


def _load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[len("export ") :].strip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _percentile(values: List[int], ratio: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = max(0, min(len(sorted_values) - 1, int((ratio / 100.0) * (len(sorted_values) - 1))))
    return float(sorted_values[idx])


def _normalize_int(value: Any, default: int) -> int:
    try:
        iv = int(value)
    except Exception:
        return default
    return iv if iv > 0 else default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect auto_trade_decisions details_json payload sizes")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="archive.db path")
    parser.add_argument("--action", default="", choices=["", "entry", "exit"], help="filter action")
    parser.add_argument("--hours", type=int, default=24, help="lookback hours, 0 means all")
    parser.add_argument("--top", type=int, default=30, help="print top large rows")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _load_dotenv()

    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db_not_found:{db_path}"}, ensure_ascii=False))
        return 1

    limit = os.environ.get("PM_DECISION_DETAILS_COMPACT_MAX_BYTES")
    try:
        compact_limit = _normalize_int(limit, 2048) if limit is not None else 2048
    except Exception:
        compact_limit = 2048

    where = "1=1"
    params: List[Any] = []
    if args.action:
        where += " AND action = ?"
        params.append(args.action)
    if args.hours > 0:
        where += " AND datetime(created_at) >= datetime('now', '-' || ? || ' hours')"
        params.append(args.hours)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            f"""
            SELECT id, action, will_order, created_at,
                   COALESCE(LENGTH(details_json), 0) AS details_bytes
            FROM auto_trade_decisions
            WHERE {where}
            ORDER BY id DESC
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    payloads = [int(row[4] or 0) for row in rows]
    if not payloads:
        print(json.dumps({"ok": True, "rows": 0, "compact_limit": compact_limit}, ensure_ascii=False, indent=2))
        return 0

    payloads_sorted = sorted(payloads)
    mean_bytes = mean(payloads_sorted)
    p50 = _percentile(payloads_sorted, 50)
    p95 = _percentile(payloads_sorted, 95)
    p99 = _percentile(payloads_sorted, 99)
    max_bytes = payloads_sorted[-1]
    top = sorted(rows, key=lambda item: int(item[4] or 0), reverse=True)[: args.top]

    payload_meta = {
        "ok": True,
        "compact_limit": compact_limit,
        "lookback_hours": args.hours,
        "action_filter": args.action or "all",
        "rows": len(rows),
        "size_bytes": {
            "min": payloads_sorted[0],
            "max": max_bytes,
            "mean": int(mean_bytes),
            "p50": int(p50),
            "p95": int(p95),
            "p99": int(p99),
            "over_limit_count": sum(1 for x in payloads if x > compact_limit),
        },
        "top": [
            {
                "id": int(row[0]),
                "action": str(row[1]),
                "will_order": 1 if int(row[2] or 0) == 1 else 0,
                "created_at": str(row[3] or ""),
                "details_bytes": int(row[4] or 0),
            }
            for row in top
        ],
        "action_summary": {
            "entry_rows": sum(1 for row in rows if str(row[1] or "") == "entry"),
            "exit_rows": sum(1 for row in rows if str(row[1] or "") == "exit"),
            "entry_will_order": sum(
                1 for row in rows if str(row[1] or "") == "entry" and int(row[2] or 0) == 1
            ),
            "entry_no_order": sum(
                1 for row in rows if str(row[1] or "") == "entry" and int(row[2] or 0) != 1
            ),
            "exit_will_order": sum(
                1 for row in rows if str(row[1] or "") == "exit" and int(row[2] or 0) == 1
            ),
            "exit_no_order": sum(
                1 for row in rows if str(row[1] or "") == "exit" and int(row[2] or 0) != 1
            ),
        },
    }

    print(json.dumps(payload_meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
