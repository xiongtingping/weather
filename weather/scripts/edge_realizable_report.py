#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


repo_root = _repo_root()
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from edge_realizable import edge_realizable_bucket_key, _default_bins  # noqa: E402

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


def _days_to_settle(event_date: str, ts: datetime) -> Optional[float]:
    if not event_date or ts is None:
        return None
    try:
        event_day = datetime.strptime(event_date[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return max((event_day - ts.date()).days, 0)


def _nearest_tick_meta(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    ts: datetime,
) -> Tuple[Optional[float], Optional[float]]:
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


def _sum_entry_fill_size(conn: sqlite3.Connection, order_id: str) -> Optional[float]:
    if not order_id:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT SUM(size)
            FROM edge_trade_fills
            WHERE order_id = ?
              AND UPPER(COALESCE(trade_side, '')) = 'BUY'
            """,
            (order_id,),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return _safe_float(row[0])


def _pick_entry_order(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
) -> Optional[sqlite3.Row]:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT o.order_id, o.created_at, o.price, o.size, o.order_type, o.q_edge
            FROM auto_orders o
            JOIN edge_trade_fills f ON o.order_id = f.order_id
            WHERE o.action = 'entry'
              AND o.slug = ? AND o.event_date = ? AND o.outcome = ? AND o.side = ?
              AND UPPER(COALESCE(f.trade_side, '')) = 'BUY'
            ORDER BY o.created_at ASC
            LIMIT 1
            """,
            (slug, event_date, outcome, side),
        )
        return cur.fetchone()
    except sqlite3.OperationalError:
        return None


def _pick_decision(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    ts: datetime,
) -> Optional[Dict[str, Any]]:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT details_json
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND slug = ? AND event_date = ? AND outcome = ? AND side = ?
              AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (slug, event_date, outcome, side, ts.isoformat(timespec="seconds")),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return json.loads(row[0] or "{}")
    except Exception:
        return None


def _pred_edge_from_decision(decision: Optional[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    if not isinstance(decision, dict):
        return None, "missing"
    edge_realizable = _safe_float(decision.get("edge_realizable"))
    if edge_realizable is not None:
        return edge_realizable, "edge_realizable"
    exec_edge_entry = _safe_float(decision.get("exec_edge_entry"))
    if exec_edge_entry is not None:
        return exec_edge_entry, "exec_edge_entry"
    return None, "missing"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(args.since_days)))
    since_iso = since_dt.isoformat(timespec="seconds")

    bins = _default_bins()
    totals = {
        "filled_entries": 0,
        "with_trade": 0,
        "with_pred_edge": 0,
        "skipped_before_since": 0,
        "skipped_no_order": 0,
        "skipped_no_trade": 0,
        "skipped_no_pred_edge": 0,
        "skipped_no_entry_size": 0,
    }
    source_counts: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"count": 0, "pnl": 0.0, "pred": 0.0, "real": 0.0, "win": 0}
    )
    bucket_stats: Dict[str, Dict[str, Any]] = {}
    total_pnl = 0.0
    total_size = 0.0
    total_pred = 0.0
    total_real = 0.0
    total_wins = 0

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT slug, event_date, outcome, side, entry_price, entry_size, realized_pnl
            FROM edge_trades
            WHERE realized_pnl IS NOT NULL
              AND entry_price IS NOT NULL
              AND entry_size IS NOT NULL
              AND entry_size > 0
              AND (entry_at >= ? OR entry_at IS NULL)
            """,
            (since_iso,),
        )
        trades = cur.fetchall()

        for trade in trades:
            slug = str(trade["slug"] or "")
            event_date = str(trade["event_date"] or "")
            outcome = str(trade["outcome"] or "")
            side = str(trade["side"] or "").upper()
            if not slug or not event_date or not outcome or side not in {"YES", "NO"}:
                continue

            order = _pick_entry_order(conn, slug, event_date, outcome, side)
            if not order:
                totals["skipped_no_order"] += 1
                continue
            order_time = _parse_iso(order["created_at"] or "")
            if order_time is None:
                totals["skipped_no_order"] += 1
                continue
            if order_time < since_dt:
                totals["skipped_before_since"] += 1
                continue
            totals["filled_entries"] += 1

            decision = _pick_decision(conn, slug, event_date, outcome, side, order_time)
            pred_edge, pred_source = _pred_edge_from_decision(decision)
            if pred_edge is None:
                q_edge = _safe_float(order["q_edge"])
                price = _safe_float(order["price"])
                if q_edge is not None and price is not None:
                    pred_edge = q_edge - price
                    pred_source = "q_edge_price"
            if pred_edge is None:
                totals["skipped_no_pred_edge"] += 1
                continue

            entry_size = _safe_float(trade["entry_size"])
            if entry_size is None or entry_size <= 0:
                entry_size = _sum_entry_fill_size(conn, str(order["order_id"] or ""))
            if entry_size is None or entry_size <= 0:
                totals["skipped_no_entry_size"] += 1
                continue

            realized_pnl = _safe_float(trade["realized_pnl"])
            if realized_pnl is None:
                totals["skipped_no_trade"] += 1
                continue

            totals["with_trade"] += 1
            totals["with_pred_edge"] += 1

            realized_edge = realized_pnl / entry_size if entry_size else None
            if realized_edge is None or not math.isfinite(realized_edge):
                totals["skipped_no_trade"] += 1
                continue

            spread, depth_top = _nearest_tick_meta(conn, slug, event_date, outcome, side, order_time)
            depth_ratio = None
            order_size = _safe_float(order["size"])
            size_ref = order_size if order_size and order_size > 0 else entry_size
            if depth_top is not None and size_ref is not None and size_ref > 0:
                depth_ratio = depth_top / size_ref

            dts = _days_to_settle(event_date, order_time)
            order_type = str(order["order_type"] or "").strip().upper() or "ANY"
            edge_val = pred_edge
            bucket_key = edge_realizable_bucket_key(
                days_to_settle=dts,
                spread=spread,
                depth_ratio=depth_ratio,
                edge_val=edge_val,
                delta_exit=None,
                order_type=order_type,
                bins=bins,
            )

            stat = bucket_stats.setdefault(
                bucket_key,
                {
                    "count": 0,
                    "pred_sum": 0.0,
                    "real_sum": 0.0,
                    "pnl_sum": 0.0,
                    "size_sum": 0.0,
                    "win": 0,
                    "sources": defaultdict(int),
                },
            )
            stat["count"] += 1
            stat["pred_sum"] += float(pred_edge)
            stat["real_sum"] += float(realized_edge)
            stat["pnl_sum"] += float(realized_pnl)
            stat["size_sum"] += float(entry_size)
            if realized_edge > 0:
                stat["win"] += 1
            stat["sources"][pred_source] += 1

            total_pnl += float(realized_pnl)
            total_size += float(entry_size)
            total_pred += float(pred_edge)
            total_real += float(realized_edge)
            if realized_edge > 0:
                total_wins += 1

            src = source_counts[pred_source]
            src["count"] += 1
            src["pnl"] += float(realized_pnl)
            src["pred"] += float(pred_edge)
            src["real"] += float(realized_edge)
            if realized_edge > 0:
                src["win"] += 1

    buckets_out = []
    for key, stat in bucket_stats.items():
        count = int(stat["count"])
        if count <= 0:
            continue
        pred_avg = stat["pred_sum"] / count
        real_avg = stat["real_sum"] / count
        win_rate = stat["win"] / count if count else 0.0
        pnl_sum = stat["pnl_sum"]
        size_avg = stat["size_sum"] / count if count else None
        buckets_out.append(
            {
                "bucket": key,
                "count": count,
                "avg_pred_edge": pred_avg,
                "avg_real_edge": real_avg,
                "bias": real_avg - pred_avg,
                "win_rate": win_rate,
                "pnl_sum": pnl_sum,
                "pnl_share": (pnl_sum / total_pnl) if total_pnl else 0.0,
                "avg_entry_size": size_avg,
                "low_sample": count < int(args.min_samples),
                "sources": dict(stat["sources"]),
            }
        )

    buckets_out.sort(key=lambda item: (item["pnl_sum"], item["count"]), reverse=True)
    sources_out = []
    for src, stat in source_counts.items():
        count = int(stat["count"])
        if count <= 0:
            continue
        sources_out.append(
            {
                "source": src,
                "count": count,
                "avg_pred_edge": stat["pred"] / count,
                "avg_real_edge": stat["real"] / count,
                "win_rate": (stat["win"] / count) if count else 0.0,
                "pnl_sum": stat["pnl"],
            }
        )

    out = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_iso,
        "counts": totals,
        "overall": {
            "avg_pred_edge": (total_pred / totals["with_pred_edge"]) if totals["with_pred_edge"] else None,
            "avg_real_edge": (total_real / totals["with_pred_edge"]) if totals["with_pred_edge"] else None,
            "win_rate": (total_wins / totals["with_pred_edge"]) if totals["with_pred_edge"] else None,
            "total_pnl": total_pnl,
            "total_entry_size": total_size,
        },
        "sources": sources_out,
        "buckets": buckets_out,
        "notes": {
            "pred_edge_source": {
                "edge_realizable": "decision.details_json.edge_realizable",
                "exec_edge_entry": "decision.details_json.exec_edge_entry",
                "q_edge_price": "auto_orders.q_edge - auto_orders.price",
            },
            "bucket_schema": "edge_realizable_bucket_key(days_to_settle, spread, depth_ratio, edge_val, delta_exit=None, order_type)",
        },
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
