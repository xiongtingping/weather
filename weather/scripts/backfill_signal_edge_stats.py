#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "data" / "archive.db"


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def compute_signal_edge_stats(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "total_candidates": 0,
        "passed": 0,
        "reasons": {},
        "min_models": None,
        "model_count": None,
        "min_model_spread": None,
        "min_buy_q": None,
        "max_buy_q": None,
        "max_buy_ask": None,
        "max_spread": None,
        "edge_formula": "q_raw - best_ask",
        "edge_mode": "signal_raw",
    }
    reasons: Dict[str, int] = {}

    def bump(key: str) -> None:
        reasons[key] = reasons.get(key, 0) + 1

    for row in rows or []:
        for side in ("yes", "no"):
            stats["total_candidates"] += 1
            if side == "yes":
                q_val = row.get("q_raw")
                if q_val is None:
                    q_val = row.get("q_round")
                best_ask = row.get("best_ask")
            else:
                q_val = row.get("q_raw_no")
                if q_val is None:
                    q_val = row.get("q_round_no")
                if q_val is None and is_finite(row.get("q_raw")):
                    q_val = 1.0 - float(row.get("q_raw"))
                if q_val is None and is_finite(row.get("q_round")):
                    q_val = 1.0 - float(row.get("q_round"))
                best_ask = row.get("best_ask_no")
            if not is_finite(q_val):
                bump("q_missing")
                continue
            if not is_finite(best_ask):
                bump("ask_missing")
                continue
            signal_edge = float(q_val) - float(best_ask)
            if signal_edge <= 0:
                bump("edge_nonpositive")
                continue
            stats["passed"] += 1

    stats["reasons"] = reasons
    return stats


def should_update(payload: Dict[str, Any], rewrite_all: bool) -> bool:
    if rewrite_all:
        return True
    stats = payload.get("suggestion_filter_stats")
    if not stats:
        return True
    if isinstance(stats, dict) and stats.get("edge_mode") != "signal_raw":
        return True
    return False


def iter_event_runs(conn: sqlite3.Connection, limit: int | None) -> Iterable[sqlite3.Row]:
    sql = "SELECT id, payload_json FROM event_runs WHERE payload_json IS NOT NULL"
    params: Tuple[Any, ...] = ()
    if limit and limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    cur = conn.cursor()
    cur.execute(sql, params)
    for row in cur:
        yield row


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill suggestion_filter_stats (pure signal edge) into event_runs payload_json."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to archive.db")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of rows (0=all)")
    parser.add_argument("--batch", type=int, default=200, help="Commit every N updates")
    parser.add_argument("--dry-run", action="store_true", help="Only report, do not update")
    parser.add_argument(
        "--rewrite-all",
        action="store_true",
        help="Rewrite stats even if already present (or edge_mode != signal_raw)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    updated = 0
    scanned = 0
    skipped = 0
    failed = 0

    conn = open_db(db_path)
    try:
        cur = conn.cursor()
        for row in iter_event_runs(conn, args.limit or None):
            scanned += 1
            row_id = row["id"]
            payload_raw = row["payload_json"]
            try:
                payload = json.loads(payload_raw)
            except Exception:
                failed += 1
                continue
            if not should_update(payload, args.rewrite_all):
                skipped += 1
                continue
            rows = payload.get("rows") or []
            stats = compute_signal_edge_stats(rows)
            payload["suggestion_filter_stats"] = stats
            if args.dry_run:
                updated += 1
                continue
            new_payload = json.dumps(payload, separators=(",", ":"))
            cur.execute(
                "UPDATE event_runs SET payload_json = ? WHERE id = ?",
                (new_payload, row_id),
            )
            updated += 1
            if updated % max(1, args.batch) == 0:
                conn.commit()
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "scanned": scanned,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
