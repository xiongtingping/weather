#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _parse_iso(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_date_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0]
    return text


def _infer_action_from_trade_side(trade_side: str) -> str:
    side = str(trade_side or "").strip().upper()
    if side == "BUY":
        return "entry"
    if side == "SELL":
        return "exit"
    return ""


def _load_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair trade_fill_attributions by inferring auto_orders via key+time matching."
    )
    parser.add_argument("--db", default="data/archive.db", help="SQLite path (default: data/archive.db)")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument(
        "--max-delta-hours",
        type=float,
        default=168.0,
        help="Max |fill_time - auto_order.created_at| in hours (default: 168)",
    )
    parser.add_argument(
        "--reason",
        action="append",
        default=["no_auto_order"],
        help="Attribution reason(s) to repair (repeatable, default: no_auto_order)",
    )
    parser.add_argument(
        "--allow-action-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow fallback to same-key auto_order when entry/exit action match is absent (default: true)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    reasons = sorted({str(r or "").strip() for r in (args.reason or []) if str(r or "").strip()})
    if not reasons:
        reasons = ["no_auto_order"]
    max_delta_sec = max(0.0, float(args.max_delta_hours) * 3600.0)
    limit = max(0, int(args.limit or 0))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    reason_placeholders = ",".join(["?"] * len(reasons))
    sql = f"""
        SELECT
            f.id AS fill_id,
            f.trade_id,
            f.order_id,
            f.slug,
            f.event_date,
            f.outcome,
            f.outcome_side AS side,
            upper(COALESCE(f.trade_side, '')) AS trade_side,
            COALESCE(f.trade_time, f.created_at) AS trade_ts,
            a.reason,
            a.evidence_json
        FROM edge_trade_fills f
        JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
        WHERE COALESCE(a.linked, 0) <> 1
          AND COALESCE(f.order_id, '') <> ''
          AND COALESCE(a.reason, '') IN ({reason_placeholders})
        ORDER BY datetime(COALESCE(f.trade_time, f.created_at)) DESC, f.id DESC
    """
    params: List[Any] = list(reasons)
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    cur.execute(sql, tuple(params))
    rows = cur.fetchall() or []

    key_cache: Dict[Tuple[str, str, str, str], List[sqlite3.Row]] = {}

    def load_key_matches(key: Tuple[str, str, str, str]) -> List[sqlite3.Row]:
        cached = key_cache.get(key)
        if cached is not None:
            return cached
        cur.execute(
            """
            SELECT id, order_id, action, strategy, created_at
            FROM auto_orders
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND upper(COALESCE(side, '')) = upper(?)
            ORDER BY datetime(created_at) DESC, id DESC
            """,
            key,
        )
        result = cur.fetchall() or []
        key_cache[key] = result
        return result

    scanned = 0
    matched = 0
    updated = 0
    skipped_no_match = 0
    skipped_delta = 0
    fallback_used = 0

    for row in rows:
        scanned += 1
        slug = str(row["slug"] or "").strip()
        event_date = _normalize_date_key(str(row["event_date"] or "").strip())
        outcome = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").strip().upper()
        trade_side = str(row["trade_side"] or "").strip().upper()
        trade_ts_text = str(row["trade_ts"] or "").strip()
        trade_dt = _parse_iso(trade_ts_text)
        key = (slug, event_date, outcome, side)
        candidates = load_key_matches(key)
        if not candidates:
            skipped_no_match += 1
            continue
        action_need = _infer_action_from_trade_side(trade_side)
        action_matched = []
        if action_need:
            action_matched = [
                c for c in candidates if str(c["action"] or "").strip().lower() == action_need
            ]
        pool = action_matched
        if not pool:
            if not args.allow_action_fallback:
                skipped_no_match += 1
                continue
            pool = candidates
            fallback_used += 1
        best_row: Optional[sqlite3.Row] = None
        best_delta: Optional[float] = None
        for cand in pool:
            created_dt = _parse_iso(str(cand["created_at"] or ""))
            if trade_dt is None or created_dt is None:
                delta = 0.0
            else:
                delta = abs((trade_dt - created_dt).total_seconds())
            if best_row is None or best_delta is None or delta < best_delta:
                best_row = cand
                best_delta = delta
        if best_row is None:
            skipped_no_match += 1
            continue
        if best_delta is not None and max_delta_sec > 0 and best_delta > max_delta_sec:
            skipped_delta += 1
            continue
        matched += 1

        evidence = _load_json(row["evidence_json"])
        evidence["inferred_link"] = {
            "method": "key_time",
            "previous_reason": str(row["reason"] or ""),
            "fill_order_id": str(row["order_id"] or ""),
            "matched_order_id": str(best_row["order_id"] or ""),
            "matched_action": str(best_row["action"] or ""),
            "matched_strategy": str(best_row["strategy"] or ""),
            "matched_created_at": str(best_row["created_at"] or ""),
            "delta_sec": float(best_delta or 0.0),
            "action_need": action_need,
            "action_fallback": bool(not action_matched),
            "repaired_at": _utc_now_iso(),
        }

        if args.dry_run:
            continue

        cur.execute(
            """
            UPDATE trade_fill_attributions
            SET auto_order_id = ?,
                auto_order_action = ?,
                auto_order_strategy = ?,
                linked = 1,
                reason = ?,
                evidence_json = ?,
                attributed_at = ?
            WHERE trade_id = ?
            """,
            (
                int(best_row["id"]) if best_row["id"] is not None else None,
                str(best_row["action"] or "").strip().lower(),
                str(best_row["strategy"] or "").strip(),
                "linked_inferred_key",
                json.dumps(evidence, ensure_ascii=False),
                _utc_now_iso(),
                str(row["trade_id"] or ""),
            ),
        )
        if cur.rowcount > 0:
            updated += 1

    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()

    post = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN linked = 1 THEN 1 ELSE 0 END) AS linked,
            SUM(CASE WHEN linked <> 1 THEN 1 ELSE 0 END) AS unlinked
        FROM trade_fill_attributions
        """
    ).fetchone()
    reason_breakdown = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(reason), ''), 'unknown') AS reason, COUNT(*) AS n
        FROM trade_fill_attributions
        GROUP BY reason
        ORDER BY n DESC, reason ASC
        LIMIT 12
        """
    ).fetchall()

    summary = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "db": str(db_path),
        "reasons": reasons,
        "limit": limit,
        "max_delta_hours": float(args.max_delta_hours),
        "allow_action_fallback": bool(args.allow_action_fallback),
        "scanned": scanned,
        "matched": matched,
        "updated": updated,
        "fallback_used": fallback_used,
        "skipped_no_match": skipped_no_match,
        "skipped_delta": skipped_delta,
        "post_total": int((post["total"] if post else 0) or 0),
        "post_linked": int((post["linked"] if post else 0) or 0),
        "post_unlinked": int((post["unlinked"] if post else 0) or 0),
        "top_reasons": [
            {"reason": str(r["reason"] or "unknown"), "count": int(r["n"] or 0)}
            for r in (reason_breakdown or [])
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main()
