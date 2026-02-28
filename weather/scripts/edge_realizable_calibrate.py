#!/usr/bin/env python3
import argparse
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from edge_realizable import edge_realizable_bucket_key, _default_bins

DB_DEFAULT = str(Path(__file__).resolve().parent.parent / "data" / "archive.db")


def _parse_iso(ts: str) -> Optional[datetime]:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _extract_rows(fact_json: str) -> List[Dict[str, Any]]:
    if not fact_json:
        return []
    try:
        payload = json.loads(fact_json)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("distribution") or payload.get("rows") or []
    return rows if isinstance(rows, list) else []


def _days_to_settle(event_date: str, fact_as_of: str) -> Optional[float]:
    if not event_date:
        return None
    try:
        event_day = datetime.strptime(event_date[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    fact_dt = _parse_iso(fact_as_of)
    if fact_dt is None:
        return None
    return max((event_day - fact_dt.date()).days, 0)


def _touches_target(conn: sqlite3.Connection, slug: str, event_date: str, outcome: str, side: str, t0: datetime, t1: datetime, target: float) -> Optional[bool]:
    if not slug or not event_date or not outcome or not side:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MAX(best_bid)
            FROM event_market_ticks
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
              AND created_at >= ? AND created_at <= ?
            """,
            (slug, event_date, outcome, side, t0.isoformat(timespec="seconds"), t1.isoformat(timespec="seconds")),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    best = _safe_float(row[0])
    if best is None:
        return None
    return bool(best >= target - 1e-9)


def _nearest_tick_meta(conn: sqlite3.Connection, slug: str, event_date: str, outcome: str, side: str, ts: datetime) -> Tuple[Optional[float], Optional[float]]:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT spread, ask_depth_topN
            FROM event_market_ticks
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
              AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (slug, event_date, outcome, side, ts.isoformat(timespec="seconds")),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        return None, None
    if not row:
        return None, None
    return _safe_float(row[0]), _safe_float(row[1])


def _iter_entry_orders(conn: sqlite3.Connection, since_iso: str) -> Iterable[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT order_id, slug, event_date, outcome, side, price, size, order_type, created_at
        FROM auto_orders
        WHERE action = 'entry' AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (since_iso,),
    )
    for row in cur.fetchall():
        yield {
            "order_id": str(row[0] or ""),
            "slug": str(row[1] or ""),
            "event_date": str(row[2] or ""),
            "outcome": str(row[3] or ""),
            "side": str(row[4] or "").upper(),
            "price": _safe_float(row[5]),
            "size": _safe_float(row[6]),
            "order_type": str(row[7] or "").upper(),
            "created_at": str(row[8] or ""),
        }


def _entry_filled_set(conn: sqlite3.Connection, since_iso: str) -> set:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT order_id
        FROM edge_trade_fills
        WHERE trade_time >= ? AND order_id IS NOT NULL AND order_id != ''
        """,
        (since_iso,),
    )
    return {str(r[0]) for r in cur.fetchall()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--since-days", type=int, default=14)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "edge_realizable_calibration.json"))
    ap.add_argument("--max-facts", type=int, default=3000)
    ap.add_argument("--min-hours", type=float, default=0.5)
    ap.add_argument("--max-hours", type=float, default=12.0)
    ap.add_argument("--ratio", type=float, default=0.25)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(args.since_days)))
    since_iso = since_dt.isoformat(timespec="seconds")

    bins = _default_bins()
    converge_stats: Dict[str, Dict[str, int]] = {}
    entry_stats: Dict[str, Dict[str, int]] = {}

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT slug, event_date, fact_as_of, fact_json
            FROM event_fact_history
            WHERE fact_as_of >= ?
            ORDER BY fact_as_of DESC
            LIMIT ?
            """,
            (since_iso, int(args.max_facts)),
        )
        rows = cur.fetchall()
        for row in rows:
            slug = str(row[0] or "")
            event_date = str(row[1] or "")
            fact_as_of = str(row[2] or "")
            fact_dt = _parse_iso(fact_as_of)
            if not slug or not event_date or fact_dt is None:
                continue
            dts = _days_to_settle(event_date, fact_as_of)
            if dts is None:
                continue
            hours = float(dts) * 24.0
            horizon = max(args.min_hours, min(args.max_hours, hours * args.ratio))
            t1 = fact_dt + timedelta(hours=horizon)
            fact_rows = _extract_rows(str(row[3] or ""))
            for item in fact_rows:
                if not isinstance(item, dict):
                    continue
                outcome = str(item.get("outcome") or "")
                if not outcome:
                    continue
                for side, suffix in (("YES", ""), ("NO", "_no")):
                    q_entry = _safe_float(item.get(f"q_entry{suffix}"))
                    best_bid = _safe_float(item.get(f"best_bid{suffix}"))
                    best_ask = _safe_float(item.get(f"best_ask{suffix}"))
                    spread = _safe_float(item.get(f"spread{suffix}"))
                    depth_total = _safe_float(item.get(f"ask_depth_topN{suffix}"))
                    entry_size = _safe_float(item.get(f"entry_size{suffix}"))
                    exec_edge_entry = _safe_float(item.get(f"exec_edge_entry{suffix}"))
                    exec_exit_price = _safe_float(item.get(f"exec_exit_price{suffix}"))
                    exec_edge_exit = _safe_float(item.get(f"exec_edge_exit{suffix}"))
                    if exec_edge_entry is None or best_bid is None or best_ask is None:
                        continue
                    target_exit_price = None
                    if exec_exit_price is not None and exec_edge_exit is not None:
                        target_exit_price = exec_exit_price + exec_edge_exit - 0.02
                        if target_exit_price <= 0:
                            target_exit_price = None
                    if target_exit_price is None:
                        continue
                    depth_ratio = None
                    if (
                        depth_total is not None
                        and math.isfinite(depth_total)
                        and depth_total > 0
                        and entry_size is not None
                        and math.isfinite(entry_size)
                        and entry_size > 0
                    ):
                        depth_ratio = depth_total / entry_size
                    delta_exit = None
                    if best_bid is not None:
                        delta_exit = target_exit_price - best_bid
                    key = edge_realizable_bucket_key(
                        days_to_settle=dts,
                        spread=spread,
                        depth_ratio=depth_ratio,
                        edge_val=exec_edge_entry,
                        delta_exit=delta_exit,
                        order_type=None,
                        bins=bins,
                    )
                    hit = _touches_target(conn, slug, event_date, outcome, side, fact_dt, t1, target_exit_price)
                    if hit is None:
                        continue
                    stat = converge_stats.setdefault(key, {"hit": 0, "total": 0})
                    stat["total"] += 1
                    if hit:
                        stat["hit"] += 1

        filled_orders = _entry_filled_set(conn, since_iso)
        for order in _iter_entry_orders(conn, since_iso):
            slug = order["slug"]
            event_date = order["event_date"]
            outcome = order["outcome"]
            side = order["side"]
            created_at = order["created_at"]
            order_dt = _parse_iso(created_at)
            if not slug or not event_date or not outcome or side not in {"YES", "NO"} or order_dt is None:
                continue
            spread, depth_top = _nearest_tick_meta(conn, slug, event_date, outcome, side, order_dt)
            size = order.get("size")
            depth_ratio = None
            if depth_top is not None and math.isfinite(depth_top) and size is not None and math.isfinite(size) and size > 0:
                depth_ratio = depth_top / size
            dts_entry = _days_to_settle(event_date, created_at)
            key = edge_realizable_bucket_key(
                days_to_settle=dts_entry,
                spread=spread,
                depth_ratio=depth_ratio,
                edge_val=None,
                delta_exit=None,
                order_type=order.get("order_type"),
                bins=bins,
            )
            stat = entry_stats.setdefault(key, {"filled": 0, "total": 0})
            stat["total"] += 1
            if order["order_id"] and order["order_id"] in filled_orders:
                stat["filled"] += 1

        exit_attempts = 0
        exit_filled = 0
        exit_order_ids: set = set()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT order_id
                FROM auto_orders
                WHERE action = 'exit' AND created_at >= ?
                """,
                (since_iso,),
            )
            for row in cur.fetchall() or []:
                order_id = str(row[0] or "").strip()
                if order_id:
                    exit_order_ids.add(order_id)
            exit_attempts = len(exit_order_ids)
        except sqlite3.OperationalError:
            exit_attempts = 0
        if exit_order_ids:
            try:
                cur = conn.cursor()
                placeholders = ",".join(["?"] * len(exit_order_ids))
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT order_id)
                    FROM edge_trade_fills
                    WHERE trade_time >= ?
                      AND UPPER(COALESCE(trade_side, '')) = 'SELL'
                      AND order_id IN ({placeholders})
                    """,
                    (since_iso, *exit_order_ids),
                )
                row = cur.fetchone()
                exit_filled = int(row[0] or 0) if row else 0
            except sqlite3.OperationalError:
                exit_filled = 0

    p_converge_buckets = {}
    for key, stat in converge_stats.items():
        total = int(stat.get("total") or 0)
        hit = int(stat.get("hit") or 0)
        if total <= 0:
            continue
        p_converge_buckets[key] = {
            "hit": hit,
            "total": total,
            "p": (hit + 1) / (total + 2),
        }

    p_fill_entry_buckets = {}
    for key, stat in entry_stats.items():
        total = int(stat.get("total") or 0)
        filled = int(stat.get("filled") or 0)
        if total <= 0:
            continue
        p_fill_entry_buckets[key] = {
            "filled": filled,
            "total": total,
            "p": (filled + 1) / (total + 2),
        }

    p_fill_exit_default = 1.0
    if exit_attempts > 0:
        p_fill_exit_default = max(0.0, min(1.0, (exit_filled + 1) / (exit_attempts + 2)))

    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_iso,
        "bins": bins,
        "p_converge_buckets": p_converge_buckets,
        "p_fill_entry_buckets": p_fill_entry_buckets,
        "p_fill_exit_default": p_fill_exit_default,
        "hold_cost_params": {
            "mode": "linear",
            "k_per_hour": 0.0,
        },
        "stats": {
            "converge_samples": sum(v.get("total", 0) for v in p_converge_buckets.values()),
            "entry_samples": sum(v.get("total", 0) for v in p_fill_entry_buckets.values()),
            "exit_attempts": exit_attempts,
            "exit_filled": exit_filled,
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out_path), "samples": payload.get("stats")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
