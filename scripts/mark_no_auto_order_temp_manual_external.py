#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json_object(value: Any) -> Dict[str, Any]:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mark irreparable temperature no_auto_order rows as manual_or_external."
    )
    parser.add_argument("--db", default="data/archive.db", help="SQLite path (default: data/archive.db)")
    parser.add_argument(
        "--report",
        default="data/reports/no_auto_order_temp_buckets.full.json",
        help="Bucket report path (default: data/reports/no_auto_order_temp_buckets.full.json)",
    )
    parser.add_argument(
        "--from-reason",
        default="no_auto_order",
        help="Only update rows currently with this reason (default: no_auto_order)",
    )
    parser.add_argument(
        "--to-reason",
        default="manual_or_external",
        help="Target reason (default: manual_or_external)",
    )
    parser.add_argument(
        "--bucket",
        default="irreparable_manual_or_external",
        help="Bucket value in report to apply (default: irreparable_manual_or_external)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")
    if not report_path.exists():
        raise SystemExit(f"report_missing: {report_path}")

    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    details = report_payload.get("details")
    if not isinstance(details, list):
        raise SystemExit("report_invalid: missing details[] (use --print-details output)")
    bucket_name = str(args.bucket or "").strip()
    from_reason = str(args.from_reason or "").strip()
    to_reason = str(args.to_reason or "").strip()
    trade_ids: Set[str] = set()
    row_meta: Dict[str, Dict[str, Any]] = {}
    for row in details:
        if not isinstance(row, dict):
            continue
        if str(row.get("bucket") or "").strip() != bucket_name:
            continue
        trade_id = str(row.get("trade_id") or "").strip()
        if not trade_id:
            continue
        trade_ids.add(trade_id)
        row_meta[trade_id] = {
            "bucket": str(row.get("bucket") or ""),
            "bucket_reason": str(row.get("bucket_reason") or ""),
            "slug": str(row.get("slug") or ""),
            "event_date": str(row.get("event_date") or ""),
            "order_id": str(row.get("order_id") or ""),
        }

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    scanned = 0
    matched = 0
    updated = 0
    skipped_reason = 0
    skipped_missing = 0
    now_iso = _utc_now_iso()

    for trade_id in sorted(trade_ids):
        scanned += 1
        cur.execute(
            """
            SELECT trade_id, reason, linked, evidence_json
            FROM trade_fill_attributions
            WHERE trade_id = ?
            """,
            (trade_id,),
        )
        row = cur.fetchone()
        if row is None:
            skipped_missing += 1
            continue
        matched += 1
        old_reason = str(row["reason"] or "").strip()
        if from_reason and old_reason != from_reason:
            skipped_reason += 1
            continue
        evidence = _load_json_object(row["evidence_json"])
        evidence["manual_classify"] = {
            "method": "bucket_report",
            "report_path": str(report_path),
            "bucket": str(row_meta.get(trade_id, {}).get("bucket") or bucket_name),
            "bucket_reason": str(row_meta.get(trade_id, {}).get("bucket_reason") or ""),
            "from_reason": old_reason,
            "to_reason": to_reason,
            "classified_at": now_iso,
        }
        if row_meta.get(trade_id):
            evidence["manual_classify"]["slug"] = str(row_meta[trade_id].get("slug") or "")
            evidence["manual_classify"]["event_date"] = str(row_meta[trade_id].get("event_date") or "")
            evidence["manual_classify"]["order_id"] = str(row_meta[trade_id].get("order_id") or "")
        if args.dry_run:
            continue
        cur.execute(
            """
            UPDATE trade_fill_attributions
            SET linked = 0,
                reason = ?,
                evidence_json = ?,
                attributed_at = ?
            WHERE trade_id = ?
            """,
            (
                to_reason,
                json.dumps(evidence, ensure_ascii=False),
                now_iso,
                trade_id,
            ),
        )
        if cur.rowcount > 0:
            updated += 1

    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()

    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN linked = 1 THEN 1 ELSE 0 END) AS linked,
            SUM(CASE WHEN linked <> 1 THEN 1 ELSE 0 END) AS unlinked
        FROM trade_fill_attributions
        """
    ).fetchone()
    top_reasons = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(reason), ''), 'unknown') AS reason, COUNT(*) AS n
        FROM trade_fill_attributions
        GROUP BY reason
        ORDER BY n DESC, reason ASC
        LIMIT 12
        """
    ).fetchall()
    conn.close()

    payload = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "db": str(db_path),
        "report": str(report_path),
        "bucket": bucket_name,
        "from_reason": from_reason,
        "to_reason": to_reason,
        "trade_ids_in_bucket": len(trade_ids),
        "scanned": scanned,
        "matched": matched,
        "updated": updated,
        "skipped_reason": skipped_reason,
        "skipped_missing": skipped_missing,
        "post_total": int((totals["total"] if totals else 0) or 0),
        "post_linked": int((totals["linked"] if totals else 0) or 0),
        "post_unlinked": int((totals["unlinked"] if totals else 0) or 0),
        "top_reasons": [
            {"reason": str(row["reason"] or "unknown"), "count": int(row["n"] or 0)}
            for row in (top_reasons or [])
        ],
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
