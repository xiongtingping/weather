#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _parse_iso(value: Any) -> Optional[datetime]:
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


def _normalize_date_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0]
    return text


def _normalize_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    if action in {"entry_topup", "entry_improve"}:
        return "entry"
    if action == "exit_reduce":
        return "exit"
    return action


def _infer_action_from_trade_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side == "BUY":
        return "entry"
    if side == "SELL":
        return "exit"
    return ""


def _is_temp_slug(slug: Any, prefix: str) -> bool:
    text = str(slug or "").strip().lower()
    px = str(prefix or "").strip().lower()
    if not text:
        return False
    if px and text.startswith(px):
        return True
    return False


def _pick_best_by_time(
    rows: List[Dict[str, Any]],
    *,
    action_need: str,
    anchor_dt: Optional[datetime],
) -> Tuple[Optional[Dict[str, Any]], Optional[float], bool, int]:
    if not rows:
        return None, None, False, 0
    action_filtered = rows
    action_fallback = False
    if action_need in {"entry", "exit"}:
        action_filtered = [row for row in rows if _normalize_action(row.get("action")) == action_need]
        if not action_filtered:
            action_filtered = rows
            action_fallback = True
    best_row: Optional[Dict[str, Any]] = None
    best_rank: Optional[Tuple[int, float, int]] = None
    best_delta: Optional[float] = None
    for row in action_filtered:
        created_dt = _parse_iso(row.get("created_at"))
        delta_sec: Optional[float] = None
        if anchor_dt is not None and created_dt is not None:
            delta_sec = abs((anchor_dt - created_dt).total_seconds())
        rank = (
            1 if delta_sec is None else 0,
            float(delta_sec or 0.0),
            -int(row.get("id") or 0),
        )
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_row = row
            best_delta = delta_sec
    return best_row, best_delta, action_fallback, len(action_filtered)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bucket no_auto_order attributions for temperature events into reparable/irreparable."
    )
    parser.add_argument("--db", default="data/archive.db", help="SQLite path (default: data/archive.db)")
    parser.add_argument("--reason", action="append", default=["no_auto_order"], help="Attribution reason filter")
    parser.add_argument(
        "--slug-prefix",
        default="highest-temperature-in-",
        help="Only include slugs starting with this prefix (default: highest-temperature-in-)",
    )
    parser.add_argument(
        "--max-auto-order-delta-hours",
        type=float,
        default=168.0,
        help="Max fill-vs-auto_order time delta in hours for reparable bucket (default: 168)",
    )
    parser.add_argument(
        "--max-decision-delta-hours",
        type=float,
        default=24.0,
        help="Max fill-vs-decision time delta in hours for reparable bucket (default: 24)",
    )
    parser.add_argument(
        "--output",
        default="data/reports/no_auto_order_temp_buckets.json",
        help="Output JSON path (default: data/reports/no_auto_order_temp_buckets.json)",
    )
    parser.add_argument(
        "--print-details",
        action="store_true",
        help="Print full detail rows in stdout payload",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    reasons = sorted({str(r or "").strip() for r in (args.reason or []) if str(r or "").strip()})
    if not reasons:
        reasons = ["no_auto_order"]
    reason_placeholders = ",".join(["?"] * len(reasons))

    max_auto_order_delta_sec = max(0.0, float(args.max_auto_order_delta_hours) * 3600.0)
    max_decision_delta_sec = max(0.0, float(args.max_decision_delta_hours) * 3600.0)
    slug_prefix = str(args.slug_prefix or "").strip()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        f"""
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
            a.reason
        FROM edge_trade_fills f
        JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
        WHERE COALESCE(a.linked, 0) <> 1
          AND COALESCE(f.order_id, '') <> ''
          AND COALESCE(a.reason, '') IN ({reason_placeholders})
        ORDER BY datetime(COALESCE(f.trade_time, f.created_at)) DESC, f.id DESC
        """,
        tuple(reasons),
    )
    rows = cur.fetchall() or []

    order_decision_cache: Dict[str, List[Dict[str, Any]]] = {}
    auto_order_key_cache: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    decision_key_cache: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}

    def load_decisions_by_order_id(order_id: str) -> List[Dict[str, Any]]:
        key = str(order_id or "").strip()
        cached = order_decision_cache.get(key)
        if cached is not None:
            return cached
        if not key:
            order_decision_cache[key] = []
            return []
        cur.execute(
            """
            SELECT id, run_id, slug, event_date, outcome, upper(COALESCE(side, '')) AS side, action, created_at
            FROM auto_trade_decisions
            WHERE will_order = 1
              AND json_extract(details_json, '$.order_id') = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 40
            """,
            (key,),
        )
        result = [
            {
                "id": int(row["id"]) if row["id"] is not None else None,
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "slug": str(row["slug"] or "").strip(),
                "event_date": _normalize_date_key(row["event_date"]),
                "outcome": str(row["outcome"] or "").strip(),
                "side": str(row["side"] or "").strip().upper(),
                "action": _normalize_action(row["action"]),
                "created_at": str(row["created_at"] or "").strip(),
            }
            for row in (cur.fetchall() or [])
        ]
        order_decision_cache[key] = result
        return result

    def load_auto_orders_by_key(key: Tuple[str, str, str, str]) -> List[Dict[str, Any]]:
        cached = auto_order_key_cache.get(key)
        if cached is not None:
            return cached
        slug, event_date, outcome, side = key
        cur.execute(
            """
            SELECT id, order_id, action, strategy, created_at
            FROM auto_orders
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND upper(COALESCE(side, '')) = upper(?)
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 240
            """,
            (slug, event_date, outcome, side),
        )
        result = [
            {
                "id": int(row["id"]) if row["id"] is not None else None,
                "order_id": str(row["order_id"] or "").strip(),
                "action": _normalize_action(row["action"]),
                "strategy": str(row["strategy"] or "").strip(),
                "created_at": str(row["created_at"] or "").strip(),
            }
            for row in (cur.fetchall() or [])
        ]
        auto_order_key_cache[key] = result
        return result

    def load_decisions_by_key(key: Tuple[str, str, str, str]) -> List[Dict[str, Any]]:
        cached = decision_key_cache.get(key)
        if cached is not None:
            return cached
        slug, event_date, outcome, side = key
        cur.execute(
            """
            SELECT id, run_id, action, created_at, json_extract(details_json, '$.order_id') AS decision_order_id
            FROM auto_trade_decisions
            WHERE will_order = 1
              AND slug = ?
              AND (event_date = ? OR substr(event_date, 1, 10) = ?)
              AND outcome = ?
              AND upper(COALESCE(side, '')) = upper(?)
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 240
            """,
            (slug, event_date, event_date, outcome, side),
        )
        result = [
            {
                "id": int(row["id"]) if row["id"] is not None else None,
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "action": _normalize_action(row["action"]),
                "created_at": str(row["created_at"] or "").strip(),
                "decision_order_id": str(row["decision_order_id"] or "").strip(),
            }
            for row in (cur.fetchall() or [])
        ]
        decision_key_cache[key] = result
        return result

    details: List[Dict[str, Any]] = []
    non_temp_ignored = 0
    reparable = 0
    irreparable = 0
    reparable_reason_counts: Dict[str, int] = {}
    irreparable_reason_counts: Dict[str, int] = {}

    for row in rows:
        slug = str(row["slug"] or "").strip()
        if not _is_temp_slug(slug, slug_prefix):
            non_temp_ignored += 1
            continue
        trade_ts = str(row["trade_ts"] or "").strip()
        trade_dt = _parse_iso(trade_ts)
        event_date = _normalize_date_key(row["event_date"])
        outcome = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").strip().upper()
        order_id = str(row["order_id"] or "").strip()
        trade_side = str(row["trade_side"] or "").strip().upper()
        action_need = _infer_action_from_trade_side(trade_side)
        key = (slug, event_date, outcome, side)

        bucket = "irreparable_manual_or_external"
        bucket_reason = "no_internal_trace"
        repair_hint: Dict[str, Any] = {}

        decision_by_order = load_decisions_by_order_id(order_id)
        if decision_by_order:
            bucket = "reparable"
            bucket_reason = "decision_order_id_match"
            latest = decision_by_order[0]
            repair_hint = {
                "decision_id": latest.get("id"),
                "decision_run_id": latest.get("run_id"),
                "decision_created_at": latest.get("created_at"),
                "decision_action": latest.get("action"),
            }
        else:
            key_auto_orders = load_auto_orders_by_key(key)
            best_auto_row, auto_delta_sec, auto_action_fallback, auto_pool_count = _pick_best_by_time(
                key_auto_orders,
                action_need=action_need,
                anchor_dt=trade_dt,
            )
            if (
                best_auto_row is not None
                and (auto_delta_sec is None or auto_delta_sec <= max_auto_order_delta_sec)
            ):
                bucket = "reparable"
                bucket_reason = "auto_order_key_time_match"
                repair_hint = {
                    "matched_order_id": best_auto_row.get("order_id"),
                    "matched_auto_order_id": best_auto_row.get("id"),
                    "matched_action": best_auto_row.get("action"),
                    "matched_strategy": best_auto_row.get("strategy"),
                    "matched_created_at": best_auto_row.get("created_at"),
                    "delta_sec": auto_delta_sec,
                    "action_fallback": bool(auto_action_fallback),
                    "candidate_count": len(key_auto_orders),
                    "pool_count": auto_pool_count,
                }
            else:
                key_decisions = load_decisions_by_key(key)
                best_decision, decision_delta_sec, decision_action_fallback, decision_pool_count = _pick_best_by_time(
                    key_decisions,
                    action_need=action_need,
                    anchor_dt=trade_dt,
                )
                if (
                    best_decision is not None
                    and (decision_delta_sec is None or decision_delta_sec <= max_decision_delta_sec)
                ):
                    bucket = "reparable"
                    bucket_reason = "decision_key_time_match"
                    repair_hint = {
                        "decision_id": best_decision.get("id"),
                        "decision_run_id": best_decision.get("run_id"),
                        "decision_created_at": best_decision.get("created_at"),
                        "decision_action": best_decision.get("action"),
                        "decision_order_id": best_decision.get("decision_order_id"),
                        "delta_sec": decision_delta_sec,
                        "action_fallback": bool(decision_action_fallback),
                        "candidate_count": len(key_decisions),
                        "pool_count": decision_pool_count,
                    }
                else:
                    if key_auto_orders and best_auto_row is not None:
                        bucket_reason = "auto_order_key_match_but_delta_too_large"
                        repair_hint = {
                            "delta_sec": auto_delta_sec,
                            "max_delta_sec": max_auto_order_delta_sec,
                            "candidate_count": len(key_auto_orders),
                        }
                    elif key_decisions and best_decision is not None:
                        bucket_reason = "decision_key_match_but_delta_too_large"
                        repair_hint = {
                            "delta_sec": decision_delta_sec,
                            "max_delta_sec": max_decision_delta_sec,
                            "candidate_count": len(key_decisions),
                        }
                    elif key_auto_orders:
                        bucket_reason = "auto_order_key_match_but_action_missing"
                        repair_hint = {"candidate_count": len(key_auto_orders)}
                    elif key_decisions:
                        bucket_reason = "decision_key_match_but_action_missing"
                        repair_hint = {"candidate_count": len(key_decisions)}

        if bucket == "reparable":
            reparable += 1
            reparable_reason_counts[bucket_reason] = reparable_reason_counts.get(bucket_reason, 0) + 1
        else:
            irreparable += 1
            irreparable_reason_counts[bucket_reason] = irreparable_reason_counts.get(bucket_reason, 0) + 1

        details.append(
            {
                "fill_id": int(row["fill_id"]) if row["fill_id"] is not None else None,
                "trade_id": str(row["trade_id"] or "").strip(),
                "order_id": order_id,
                "slug": slug,
                "event_date": event_date,
                "outcome": outcome,
                "side": side,
                "trade_side": trade_side,
                "trade_ts": trade_ts,
                "bucket": bucket,
                "bucket_reason": bucket_reason,
                "repair_hint": repair_hint,
            }
        )

    details.sort(key=lambda item: (item["bucket"] != "reparable", item["trade_ts"]), reverse=True)

    payload: Dict[str, Any] = {
        "ok": True,
        "db": str(db_path),
        "reasons": reasons,
        "slug_prefix": slug_prefix,
        "max_auto_order_delta_hours": float(args.max_auto_order_delta_hours),
        "max_decision_delta_hours": float(args.max_decision_delta_hours),
        "totals": {
            "scanned_rows": len(rows),
            "non_temp_ignored": int(non_temp_ignored),
            "temp_rows": int(len(details)),
            "reparable": int(reparable),
            "irreparable_manual_or_external": int(irreparable),
        },
        "reparable_reason_counts": dict(sorted(reparable_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "irreparable_reason_counts": dict(
            sorted(irreparable_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
    }
    if args.print_details:
        payload["details"] = details
    else:
        payload["details_preview"] = details[:25]

    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["output"] = str(output_path)
    print(json.dumps(payload, ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main()
