#!/usr/bin/env python3
"""Read-only historical report for weather strategy candidates.

The report uses auto_trade_decisions as the execution SSOT and event_runs
settlements for outcome labels. It never writes to archive.db.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "archive.db"
KIND_SQL = """
COALESCE(
  NULLIF(json_extract(details_json, '$.weather_strategy_kind'), ''),
  NULLIF(json_extract(details_json, '$.strategy_variant'), ''),
  NULLIF(json_extract(details_json, '$.strategy'), ''),
  'unknown'
)
"""


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bucket_hit(actual: float, label: str) -> Optional[bool]:
    text = str(label or "").strip()
    if not text:
        return None
    import re

    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    low = text.lower()
    if any(token in low for token in ("or higher", "above", "+")) and nums:
        return actual >= nums[0]
    if any(token in low for token in ("or lower", "or below", "below")) and nums:
        return actual <= nums[0]
    if len(nums) >= 2:
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
        return lo <= actual <= hi
    if len(nums) == 1:
        return round(actual) == round(nums[0])
    return None


def _load_settlements_for_keys(
    conn: sqlite3.Connection,
    keys: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], float]:
    out: Dict[Tuple[str, str], float] = {}
    for slug, event_date in sorted(set(keys)):
        key = (str(slug or "").strip(), str(event_date or "").split("T", 1)[0])
        if not key[0] or not key[1]:
            continue
        try:
            row = conn.execute(
                """
                SELECT settlement_actual
                FROM event_runs
                WHERE slug = ? AND event_date = ? AND settlement_actual IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if not row:
            continue
        actual = _to_float(row[0])
        if actual is not None:
            out[key] = actual
    return out


def _load_group_rollups(conn: sqlite3.Connection, since: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "decisions": 0,
            "would_order": 0,
            "blocked": 0,
            "positive_ev": 0,
            "settled": 0,
            "settlement_hits": 0,
            "avg_profit_value": None,
            "avg_raw_ev": None,
        }
    )
    try:
        rows = conn.execute(
            f"""
            SELECT
              {KIND_SQL} AS kind,
              COUNT(*) AS decisions,
              SUM(CASE WHEN COALESCE(will_order, 0) != 0 THEN 1 ELSE 0 END) AS would_order,
              SUM(CASE WHEN lower(COALESCE(decided_state, '')) = 'blocked' THEN 1 ELSE 0 END) AS blocked,
              SUM(
                CASE
                  WHEN lower(CAST(COALESCE(json_extract(details_json, '$.weather_strategy_positive_ev'), '') AS TEXT))
                       IN ('1', 'true', 'yes', 'on')
                  THEN 1 ELSE 0
                END
              ) AS positive_ev,
              AVG(
                CAST(
                  COALESCE(
                    json_extract(details_json, '$.weather_strategy_profit_value'),
                    json_extract(details_json, '$.edge_tradeability_profit_value')
                  ) AS REAL
                )
              ) AS avg_profit_value,
              AVG(CAST(json_extract(details_json, '$.weather_strategy_raw_ev') AS REAL)) AS avg_raw_ev
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND created_at >= ?
            GROUP BY kind
            """,
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return groups
    for row in rows:
        kind = str(row[0] or "unknown")
        item = groups[kind]
        item["decisions"] = int(row[1] or 0)
        item["would_order"] = int(row[2] or 0)
        item["blocked"] = int(row[3] or 0)
        item["positive_ev"] = int(row[4] or 0)
        item["avg_profit_value"] = _to_float(row[5])
        item["avg_raw_ev"] = _to_float(row[6])
    return groups


def _load_decision_keys(conn: sqlite3.Connection, since: str) -> Iterable[Tuple[str, str]]:
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT slug, event_date
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND created_at >= ?
              AND slug IS NOT NULL
              AND event_date IS NOT NULL
            """,
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(slug or ""), str(event_date or "")) for slug, event_date in rows]


def _apply_settlement_hits(
    conn: sqlite3.Connection,
    groups: Dict[str, Dict[str, Any]],
    settlements: Mapping[Tuple[str, str], float],
    since: str,
) -> None:
    for (slug, event_date), actual in settlements.items():
        try:
            rows = conn.execute(
                f"""
                SELECT outcome, side, {KIND_SQL} AS kind
                FROM auto_trade_decisions INDEXED BY idx_auto_trade_decisions_slug_event_action_run
                WHERE slug = ?
                  AND event_date = ?
                  AND action = 'entry'
                  AND created_at >= ?
                """,
                (slug, event_date, since),
            )
        except sqlite3.OperationalError:
            continue
        for outcome, side, kind_raw in rows:
            hit = _bucket_hit(actual, str(outcome or ""))
            if hit is None:
                continue
            kind = str(kind_raw or "unknown")
            item = groups[kind]
            item["settled"] += 1
            side_u = str(side or "").upper()
            side_hit = hit if side_u != "NO" else (not hit)
            if side_hit:
                item["settlement_hits"] += 1


def build_report(db_path: Path, days: int) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    try:
        groups = _load_group_rollups(conn, since)
        settlement_keys = _load_decision_keys(conn, since)
        settlements = _load_settlements_for_keys(conn, settlement_keys)
        _apply_settlement_hits(conn, groups, settlements, since)
    finally:
        conn.close()

    summary = []
    for kind, item in sorted(groups.items(), key=lambda kv: kv[1]["decisions"], reverse=True):
        decisions = int(item["decisions"] or 0)
        settled = int(item["settled"] or 0)
        summary.append(
            {
                "kind": kind,
                "decisions": decisions,
                "would_order": int(item["would_order"] or 0),
                "blocked": int(item["blocked"] or 0),
                "positive_ev": int(item["positive_ev"] or 0),
                "positive_ev_rate": (item["positive_ev"] / decisions) if decisions else None,
                "settled": settled,
                "settlement_hit_rate": (item["settlement_hits"] / settled) if settled else None,
                "avg_profit_value": item.get("avg_profit_value"),
                "avg_raw_ev": item.get("avg_raw_ev"),
            }
        )
    return {
        "ok": True,
        "db_path": str(db_path),
        "days": int(days),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only weather strategy historical report.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(build_report(Path(args.db), args.days), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
