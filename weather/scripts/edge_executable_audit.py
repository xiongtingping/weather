#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

DB_DEFAULT = str(Path(__file__).resolve().parent.parent / "data" / "archive.db")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        val = float(raw) if raw is not None else float(default)
    except Exception:
        val = float(default)
    if not math.isfinite(val):
        return float(default)
    return float(val)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        val = int(float(raw)) if raw is not None else int(default)
    except Exception:
        val = int(default)
    return int(val)


def _parse_iso(ts: Any) -> Optional[datetime]:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_end_utc(event_date: str, tz_name: str = "America/New_York") -> Optional[datetime]:
    date_key = str(event_date or "").strip()
    if not date_key:
        return None
    if "T" in date_key:
        date_key = date_key.split("T", 1)[0]
    elif " " in date_key:
        date_key = date_key.split(" ", 1)[0]
    try:
        day = datetime.strptime(date_key, "%Y-%m-%d").date()
    except Exception:
        return None
    try:
        tz = ZoneInfo(str(tz_name or "America/New_York"))
    except Exception:
        tz = ZoneInfo("America/New_York")
    end_local = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    return end_local.astimezone(timezone.utc)


def _safe_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _clamp01(val: Optional[float]) -> Optional[float]:
    if val is None or not math.isfinite(val):
        return None
    if val < 0:
        return 0.0
    if val > 1:
        return 1.0
    return float(val)


def _quantile(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    if q <= 0:
        return min(vals)
    if q >= 1:
        return max(vals)
    arr = sorted(vals)
    idx = min(len(arr) - 1, max(0, int(len(arr) * q)))
    return float(arr[idx])


def _avg(vals: Iterable[Optional[float]]) -> Optional[float]:
    arr = [float(v) for v in vals if v is not None and math.isfinite(v)]
    if not arr:
        return None
    return float(sum(arr) / len(arr))


def _corr(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x <= 0 or den_y <= 0:
        return None
    return float(num / (den_x * den_y))


def _make_placeholders(n: int) -> str:
    return ",".join(["?"] * n)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table_name or "").strip(),),
    ).fetchone()
    return row is not None


def _truthy_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_orders(conn: sqlite3.Connection, since_iso: str) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, order_id, slug, event_date, outcome, side, action, price, size,
               created_at, order_type, order_status, strategy
        FROM auto_orders
        WHERE created_at >= ?
          AND order_id IS NOT NULL
          AND order_id != ''
          AND action IN ('entry', 'exit')
        ORDER BY created_at ASC
        """,
        (since_iso,),
    )
    return list(cur.fetchall())


def _fetch_fills_by_order(
    conn: sqlite3.Connection,
    order_ids: List[str],
    since_iso: str,
) -> Dict[str, List[sqlite3.Row]]:
    out: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    if not order_ids:
        return out
    chunk = 600
    cur = conn.cursor()
    for i in range(0, len(order_ids), chunk):
        ids = order_ids[i : i + chunk]
        ph = _make_placeholders(len(ids))
        cur.execute(
            f"""
            SELECT order_id, trade_side, price, size, fee, trade_time, slug, event_date, outcome, outcome_side
            FROM edge_trade_fills
            WHERE order_id IN ({ph})
              AND trade_time >= ?
            ORDER BY trade_time ASC
            """,
            [*ids, since_iso],
        )
        for row in cur.fetchall():
            out[str(row["order_id"] or "")].append(row)
    return out


def _fetch_edge_trades_map(conn: sqlite3.Connection) -> Dict[Tuple[str, str, str, str], sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT slug, event_date, outcome, side, state, position_size, entry_size,
               realized_pnl, unrealized_pnl, updated_at, last_action, last_action_at
        FROM edge_trades
        """
    )
    out: Dict[Tuple[str, str, str, str], sqlite3.Row] = {}
    for row in cur.fetchall():
        key = (
            str(row["slug"] or ""),
            str(row["event_date"] or ""),
            str(row["outcome"] or ""),
            str(row["side"] or "").upper(),
        )
        out[key] = row
    return out


def _load_decision_for_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    action: str,
    created_at: str,
) -> Tuple[Optional[sqlite3.Row], str]:
    if order_id and _table_exists(conn, "trade_fill_attributions"):
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT d.id, d.run_date, d.created_at, d.decided_state, d.reason_codes_json, d.details_json
                FROM trade_fill_attributions a
                JOIN edge_trade_fills f ON f.trade_id = a.trade_id
                JOIN auto_trade_decisions d ON d.id = a.decision_id
                WHERE f.order_id = ?
                  AND a.decision_id IS NOT NULL
                ORDER BY
                    a.linked DESC,
                    CASE WHEN COALESCE(a.decision_run_id, 0) > 0 THEN 0 ELSE 1 END ASC,
                    datetime(COALESCE(f.trade_time, f.created_at)) DESC,
                    a.decision_id DESC
                LIMIT 1
                """,
                (order_id,),
            )
            row_attr = cur.fetchone()
            if row_attr is not None:
                return row_attr, "attribution"
        except sqlite3.OperationalError:
            pass
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, run_date, created_at, decided_state, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE slug = ?
          AND (event_date = ? OR substr(event_date, 1, 10) = ?)
          AND outcome = ?
          AND side = ?
          AND action = ?
          AND created_at <= ?
        ORDER BY
          will_order DESC,
          CASE WHEN json_extract(details_json, '$.edge_realizable_v2') IS NOT NULL THEN 0 ELSE 1 END,
          datetime(created_at) DESC,
          id DESC
        LIMIT 1
        """,
        (slug, event_date, event_date, outcome, side, action, created_at),
    )
    row = cur.fetchone()
    if row is None:
        return None, ""
    return row, "fallback"


def _tick_before(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    ts: str,
) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at, best_bid, best_ask, spread, bid_depth_topN, ask_depth_topN
        FROM event_market_ticks
        WHERE slug = ?
          AND event_date = ?
          AND outcome = ?
          AND side = ?
          AND created_at <= ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (slug, event_date, outcome, side, ts),
    )
    return cur.fetchone()


def _touched_between(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    start_ts: str,
    end_ts: str,
    action: str,
    limit_price: Optional[float],
) -> Optional[bool]:
    if limit_price is None:
        return None
    cur = conn.cursor()
    if action == "entry":
        cur.execute(
            """
            SELECT MIN(best_ask)
            FROM event_market_ticks
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND side = ?
              AND created_at >= ?
              AND created_at <= ?
            """,
            (slug, event_date, outcome, side, start_ts, end_ts),
        )
        row = cur.fetchone()
        px = _safe_float(row[0] if row else None)
        if px is None:
            return None
        return bool(px <= float(limit_price) + 1e-9)
    cur.execute(
        """
        SELECT MAX(best_bid)
        FROM event_market_ticks
        WHERE slug = ?
          AND event_date = ?
          AND outcome = ?
          AND side = ?
          AND created_at >= ?
          AND created_at <= ?
        """,
        (slug, event_date, outcome, side, start_ts, end_ts),
    )
    row = cur.fetchone()
    px = _safe_float(row[0] if row else None)
    if px is None:
        return None
    return bool(px >= float(limit_price) - 1e-9)


def _derive_target_exit_price(
    *,
    exec_exit_price: Optional[float],
    exec_edge_exit: Optional[float],
    base_bid: Optional[float],
    spread: Optional[float],
) -> Optional[float]:
    if exec_exit_price is None or not math.isfinite(exec_exit_price):
        return None
    if exec_edge_exit is None or not math.isfinite(exec_edge_exit):
        return None
    headroom = _env_float("PM_AUTO_TRADE_EXIT_HEADROOM_MIN", 0.02)
    if not math.isfinite(headroom) or headroom < 0:
        headroom = 0.02
    raw_target = float(exec_exit_price) + float(exec_edge_exit) - float(headroom)
    if not math.isfinite(raw_target) or raw_target <= 0:
        return None
    if base_bid is None or not math.isfinite(base_bid):
        return float(raw_target)

    delta_raw = float(raw_target) - float(base_bid)
    if not math.isfinite(delta_raw):
        return None
    min_delta = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MIN_DELTA", 0.0)
    if not math.isfinite(min_delta) or min_delta < 0:
        min_delta = 0.0
    max_delta = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MAX_DELTA", 0.06)
    if not math.isfinite(max_delta) or max_delta <= 0:
        max_delta = 0.06
    max_spread_mult = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MAX_SPREAD_MULT", 2.0)
    if not math.isfinite(max_spread_mult) or max_spread_mult <= 0:
        max_spread_mult = 2.0
    cap = max_delta
    if spread is not None and math.isfinite(spread) and spread > 0:
        cap = min(cap, float(spread) * float(max_spread_mult))
    delta_final = float(delta_raw)
    if delta_final < float(min_delta):
        delta_final = float(min_delta)
    if delta_final > float(cap):
        delta_final = float(cap)
    if delta_final < 0:
        delta_final = 0.0
    target = float(base_bid) + float(delta_final)
    if not math.isfinite(target) or target <= 0:
        return None
    return float(target)


@dataclass
class OrderAudit:
    order_id: str
    action: str
    strategy: str
    created_at: str
    created_day: str
    slug: str
    event_date: str
    outcome: str
    side: str
    order_price: Optional[float]
    order_size: Optional[float]
    order_status: str
    order_type: str
    fills_count: int
    fill_size: Optional[float]
    fill_vwap: Optional[float]
    fill_ratio: Optional[float]
    first_fill_at: str
    fill_latency_sec: Optional[float]
    adverse_slippage: Optional[float]
    touched_before_fill: Optional[bool]
    touched_no_fill_horizon: Optional[bool]
    tick_age_sec: Optional[float]
    decision_id: Optional[int]
    decision_source: str
    decision_state: str
    decision_reason_codes: List[str]
    edge_realizable: Optional[float]
    edge_realizable_v2: Optional[float]
    edge_realizable_p_realize: Optional[float]
    edge_realizable_target_exit_price: Optional[float]
    edge_realizable_p_realize_missing: Optional[bool]
    edge_realizable_p_realize_low_sample: Optional[bool]
    edge_realizable_p_realize_samples_min: Optional[float]
    edge_trade_state: str
    edge_trade_position_size: Optional[float]
    edge_trade_realized_pnl: Optional[float]
    edge_trade_unrealized_pnl: Optional[float]
    edge_trade_closed: Optional[bool]
    edge_trade_realized_edge: Optional[float]
    edge_trade_mtm_edge: Optional[float]

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def _build_order_audits(
    conn: sqlite3.Connection,
    *,
    since_dt: datetime,
    touch_horizon_hours: float,
) -> List[OrderAudit]:
    since_iso = since_dt.isoformat(timespec="seconds")
    orders = _fetch_orders(conn, since_iso)
    order_ids = [str(r["order_id"] or "") for r in orders if str(r["order_id"] or "")]
    fills_by_order = _fetch_fills_by_order(conn, order_ids, since_iso)
    edge_trade_map = _fetch_edge_trades_map(conn)
    now = datetime.now(timezone.utc)
    out: List[OrderAudit] = []
    for row in orders:
        order_id = str(row["order_id"] or "")
        created_at = str(row["created_at"] or "")
        created_dt = _parse_iso(created_at)
        if created_dt is None:
            continue
        slug = str(row["slug"] or "")
        event_date = str(row["event_date"] or "")
        outcome = str(row["outcome"] or "")
        side = str(row["side"] or "").upper()
        action = str(row["action"] or "").lower()
        order_price = _safe_float(row["price"])
        order_size = _safe_float(row["size"])
        strategy = str(row["strategy"] or "")
        order_status = str(row["order_status"] or "")
        order_type = str(row["order_type"] or "")
        fills = fills_by_order.get(order_id) or []

        fill_size = 0.0
        fill_notional = 0.0
        first_fill_at = ""
        fill_latency_sec: Optional[float] = None
        for f in fills:
            sz = _safe_float(f["size"]) or 0.0
            px = _safe_float(f["price"]) or 0.0
            if sz <= 0:
                continue
            fill_size += sz
            fill_notional += px * sz
            f_dt = _parse_iso(f["trade_time"])
            if f_dt is not None and not first_fill_at:
                first_fill_at = f_dt.isoformat(timespec="seconds")
                fill_latency_sec = max(0.0, (f_dt - created_dt).total_seconds())
        fill_vwap = (fill_notional / fill_size) if fill_size > 0 else None
        fill_ratio = None
        if order_size is not None and order_size > 0:
            fill_ratio = fill_size / order_size

        adverse_slippage = None
        if fill_vwap is not None and order_price is not None:
            if action == "entry":
                adverse_slippage = float(fill_vwap) - float(order_price)
            elif action == "exit":
                adverse_slippage = float(order_price) - float(fill_vwap)

        tick = _tick_before(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            side=side,
            ts=created_at,
        )
        tick_age_sec = None
        if tick:
            tick_ts = _parse_iso(tick["created_at"])
            if tick_ts is not None:
                tick_age_sec = max(0.0, (created_dt - tick_ts).total_seconds())

        touched_before_fill = None
        touched_no_fill_horizon = None
        if fill_size > 0 and first_fill_at:
            touched_before_fill = _touched_between(
                conn,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                side=side,
                start_ts=created_at,
                end_ts=first_fill_at,
                action=action,
                limit_price=order_price,
            )
        elif fill_size <= 0:
            horizon_sec = max(300.0, float(touch_horizon_hours) * 3600.0)
            end_dt = min(now, created_dt + timedelta(seconds=horizon_sec))
            touched_no_fill_horizon = _touched_between(
                conn,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                side=side,
                start_ts=created_at,
                end_ts=end_dt.isoformat(timespec="seconds"),
                action=action,
                limit_price=order_price,
            )

        decision, decision_source = _load_decision_for_order(
            conn,
            order_id=order_id,
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            side=side,
            action=action,
            created_at=created_at,
        )
        decision_id = None
        decision_state = ""
        decision_reason_codes: List[str] = []
        details: Dict[str, Any] = {}
        if decision:
            decision_id = int(decision["id"])
            decision_state = str(decision["decided_state"] or "")
            try:
                raw_codes = json.loads(decision["reason_codes_json"] or "[]")
                if isinstance(raw_codes, list):
                    decision_reason_codes = [str(x) for x in raw_codes]
            except Exception:
                decision_reason_codes = []
            try:
                parsed = json.loads(decision["details_json"] or "{}")
                if isinstance(parsed, dict):
                    details = parsed
            except Exception:
                details = {}

        edge_row = edge_trade_map.get((slug, event_date, outcome, side))
        edge_trade_state = ""
        edge_trade_position_size = None
        edge_trade_realized_pnl = None
        edge_trade_unrealized_pnl = None
        edge_trade_closed = None
        edge_trade_realized_edge = None
        edge_trade_mtm_edge = None
        if edge_row is not None:
            edge_trade_state = str(edge_row["state"] or "")
            edge_trade_position_size = _safe_float(edge_row["position_size"])
            edge_trade_realized_pnl = _safe_float(edge_row["realized_pnl"])
            edge_trade_unrealized_pnl = _safe_float(edge_row["unrealized_pnl"])
            entry_size = _safe_float(edge_row["entry_size"])
            if entry_size is not None and entry_size > 0:
                rp = edge_trade_realized_pnl or 0.0
                up = edge_trade_unrealized_pnl or 0.0
                edge_trade_realized_edge = rp / entry_size
                edge_trade_mtm_edge = (rp + up) / entry_size
            state_l = edge_trade_state.strip().lower()
            if edge_trade_position_size is not None:
                edge_trade_closed = (
                    abs(edge_trade_position_size) <= 1e-9
                    or state_l in {"closed", "flat", "exit_done", "exit"}
                )

        audit = OrderAudit(
            order_id=order_id,
            action=action,
            strategy=strategy,
            created_at=created_at,
            created_day=created_dt.date().isoformat(),
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            side=side,
            order_price=order_price,
            order_size=order_size,
            order_status=order_status,
            order_type=order_type,
            fills_count=len(fills),
            fill_size=(fill_size if fill_size > 0 else None),
            fill_vwap=fill_vwap,
            fill_ratio=fill_ratio,
            first_fill_at=first_fill_at,
            fill_latency_sec=fill_latency_sec,
            adverse_slippage=adverse_slippage,
            touched_before_fill=touched_before_fill,
            touched_no_fill_horizon=touched_no_fill_horizon,
            tick_age_sec=tick_age_sec,
            decision_id=decision_id,
            decision_source=decision_source,
            decision_state=decision_state,
            decision_reason_codes=decision_reason_codes,
            edge_realizable=_safe_float(details.get("edge_realizable")),
            edge_realizable_v2=_safe_float(details.get("edge_realizable_v2")),
            edge_realizable_p_realize=_safe_float(details.get("edge_realizable_p_realize")),
            edge_realizable_target_exit_price=_safe_float(details.get("edge_realizable_target_exit_price")),
            edge_realizable_p_realize_missing=(
                bool(details.get("edge_realizable_p_realize_missing"))
                if "edge_realizable_p_realize_missing" in details
                else None
            ),
            edge_realizable_p_realize_low_sample=(
                bool(details.get("edge_realizable_p_realize_low_sample"))
                if "edge_realizable_p_realize_low_sample" in details
                else None
            ),
            edge_realizable_p_realize_samples_min=_safe_float(details.get("edge_realizable_p_realize_samples_min")),
            edge_trade_state=edge_trade_state,
            edge_trade_position_size=edge_trade_position_size,
            edge_trade_realized_pnl=edge_trade_realized_pnl,
            edge_trade_unrealized_pnl=edge_trade_unrealized_pnl,
            edge_trade_closed=edge_trade_closed,
            edge_trade_realized_edge=edge_trade_realized_edge,
            edge_trade_mtm_edge=edge_trade_mtm_edge,
        )
        out.append(audit)
    return out


def _replay_summary(
    audits: List[OrderAudit],
    *,
    mtm_mature_hours: float,
) -> Dict[str, Any]:
    total = len(audits)
    entries = [x for x in audits if x.action == "entry"]
    exits = [x for x in audits if x.action == "exit"]
    filled = [x for x in audits if (x.fill_size is not None and x.fill_size > 0)]
    fill_ratio_vals = [x.fill_ratio for x in filled if x.fill_ratio is not None]
    latency_vals = [x.fill_latency_sec for x in filled if x.fill_latency_sec is not None]
    slippage_vals = [x.adverse_slippage for x in filled if x.adverse_slippage is not None]
    tick_age_vals = [x.tick_age_sec for x in audits if x.tick_age_sec is not None]
    touched_unfilled = [x for x in audits if x.fill_size is None and x.touched_no_fill_horizon is True]
    decision_source_counts: Dict[str, int] = {}
    for row in entries:
        key = str(row.decision_source or "").strip() or "none"
        decision_source_counts[key] = int(decision_source_counts.get(key, 0)) + 1

    by_strategy: Dict[str, Dict[str, Any]] = {}
    strat_counter: Dict[str, Counter] = defaultdict(Counter)
    for row in entries:
        sk = row.strategy or "unknown"
        strat_counter[sk]["orders"] += 1
        if row.fill_size is not None and row.fill_size > 0:
            strat_counter[sk]["filled"] += 1
        if row.edge_realizable_v2 is not None:
            strat_counter[sk]["with_v2"] += 1
        if row.edge_realizable_p_realize is not None:
            strat_counter[sk]["with_p_realize"] += 1
    for sk, cnt in strat_counter.items():
        orders = int(cnt.get("orders", 0))
        filled_n = int(cnt.get("filled", 0))
        by_strategy[sk] = {
            "orders": orders,
            "filled": filled_n,
            "fill_ratio": (filled_n / orders) if orders else None,
            "with_v2": int(cnt.get("with_v2", 0)),
            "with_p_realize": int(cnt.get("with_p_realize", 0)),
        }

    pred_vs_real_x: List[float] = []
    pred_vs_real_y: List[float] = []
    pred_vs_effective_x: List[float] = []
    pred_vs_effective_y: List[float] = []
    pred_vs_effective_source: Counter = Counter()
    pred_vs_effective_strategy: Dict[str, Counter] = defaultdict(Counter)
    now_dt = datetime.now(timezone.utc)
    mature_sec = max(0.0, float(mtm_mature_hours)) * 3600.0
    for row in entries:
        if row.edge_realizable_v2 is None:
            continue
        if row.edge_trade_realized_edge is None or row.edge_trade_closed is not True:
            continue
        pred_vs_real_x.append(float(row.edge_realizable_v2))
        pred_vs_real_y.append(float(row.edge_trade_realized_edge))

    for row in entries:
        pred_eff = row.edge_realizable_v2
        if pred_eff is None:
            pred_eff = row.edge_realizable
        if pred_eff is None:
            continue
        created_dt = _parse_iso(row.created_at)
        age_sec = None
        if created_dt is not None:
            age_sec = max(0.0, (now_dt - created_dt).total_seconds())
        strategy_key = str(row.strategy or "").strip().lower() or "unknown"
        if row.edge_trade_closed is True and row.edge_trade_realized_edge is not None:
            pred_vs_effective_x.append(float(pred_eff))
            pred_vs_effective_y.append(float(row.edge_trade_realized_edge))
            pred_vs_effective_source["closed_realized"] += 1
            pred_vs_effective_strategy[strategy_key]["n"] += 1
            pred_vs_effective_strategy[strategy_key]["closed_realized_n"] += 1
            continue
        if (
            row.edge_trade_mtm_edge is not None
            and age_sec is not None
            and age_sec + 1e-9 >= mature_sec
        ):
            pred_vs_effective_x.append(float(pred_eff))
            pred_vs_effective_y.append(float(row.edge_trade_mtm_edge))
            pred_vs_effective_source["open_mtm_mature"] += 1
            pred_vs_effective_strategy[strategy_key]["n"] += 1
            pred_vs_effective_strategy[strategy_key]["open_mtm_mature_n"] += 1

    pred_vs_effective_by_strategy = []
    for strategy_key, cnt in sorted(pred_vs_effective_strategy.items(), key=lambda kv: kv[0]):
        n = int(cnt.get("n", 0))
        pred_vs_effective_by_strategy.append(
            {
                "strategy": strategy_key,
                "n": n,
                "closed_realized_n": int(cnt.get("closed_realized_n", 0)),
                "open_mtm_mature_n": int(cnt.get("open_mtm_mature_n", 0)),
            }
        )

    threshold_joint = [
        x
        for x in entries
        if x.edge_realizable_v2 is not None
        and x.edge_realizable_p_realize is not None
        and x.edge_realizable_v2 >= 0.05
        and x.edge_realizable_p_realize >= 0.60
    ]

    return {
        "total_orders": total,
        "entry_orders": len(entries),
        "exit_orders": len(exits),
        "filled_orders": len(filled),
        "fill_ratio_all": (len(filled) / total) if total else None,
        "fill_ratio_entries": (
            len([x for x in entries if x.fill_size is not None and x.fill_size > 0]) / len(entries)
            if entries
            else None
        ),
        "fill_ratio_quantiles": {
            "p50": _quantile([float(v) for v in fill_ratio_vals if v is not None], 0.50),
            "p90": _quantile([float(v) for v in fill_ratio_vals if v is not None], 0.90),
            "p99": _quantile([float(v) for v in fill_ratio_vals if v is not None], 0.99),
        },
        "fill_latency_sec": {
            "p50": _quantile([float(v) for v in latency_vals if v is not None], 0.50),
            "p90": _quantile([float(v) for v in latency_vals if v is not None], 0.90),
            "p99": _quantile([float(v) for v in latency_vals if v is not None], 0.99),
        },
        "adverse_slippage": {
            "avg": _avg(slippage_vals),
            "p50": _quantile([float(v) for v in slippage_vals if v is not None], 0.50),
            "p90": _quantile([float(v) for v in slippage_vals if v is not None], 0.90),
        },
        "tick_age_sec": {
            "p50": _quantile([float(v) for v in tick_age_vals if v is not None], 0.50),
            "p90": _quantile([float(v) for v in tick_age_vals if v is not None], 0.90),
            "p99": _quantile([float(v) for v in tick_age_vals if v is not None], 0.99),
        },
        "unfilled_touched_horizon_count": len(touched_unfilled),
        "coverage": {
            "entry_with_decision": len([x for x in entries if x.decision_id is not None]),
            "entry_with_v2": len([x for x in entries if x.edge_realizable_v2 is not None]),
            "entry_with_p_realize": len([x for x in entries if x.edge_realizable_p_realize is not None]),
            "entry_with_target_exit": len(
                [x for x in entries if x.edge_realizable_target_exit_price is not None]
            ),
            "entry_decision_source_counts": decision_source_counts,
        },
        "prediction_vs_realized_closed": {
            "n": len(pred_vs_real_x),
            "corr_v2_vs_real_edge": _corr(pred_vs_real_x, pred_vs_real_y),
            "avg_pred_v2": _avg(pred_vs_real_x),
            "avg_realized_edge": _avg(pred_vs_real_y),
        },
        "prediction_vs_effective_matured": {
            "n": len(pred_vs_effective_x),
            "corr_pred_vs_effective_edge": _corr(pred_vs_effective_x, pred_vs_effective_y),
            "avg_pred_edge": _avg(pred_vs_effective_x),
            "avg_effective_edge": _avg(pred_vs_effective_y),
            "mtm_mature_hours": float(max(0.0, mtm_mature_hours)),
            "source_counts": {
                "closed_realized": int(pred_vs_effective_source.get("closed_realized", 0)),
                "open_mtm_mature": int(pred_vs_effective_source.get("open_mtm_mature", 0)),
            },
            "by_strategy": pred_vs_effective_by_strategy,
        },
        "threshold_joint_v2_0_05_p_0_60": {
            "n": len(threshold_joint),
            "share_of_entries": (len(threshold_joint) / len(entries)) if entries else None,
        },
        "by_strategy": by_strategy,
    }


def _fills_linkage_summary(conn: sqlite3.Connection, *, since_iso: str) -> Dict[str, Any]:
    cur = conn.cursor()
    fills_total = int(
        cur.execute(
            """
            SELECT COUNT(*)
            FROM edge_trade_fills
            WHERE datetime(COALESCE(trade_time, created_at)) >= datetime(?)
            """,
            (since_iso,),
        ).fetchone()[0]
        or 0
    )
    fills_with_order_id = int(
        cur.execute(
            """
            SELECT COUNT(*)
            FROM edge_trade_fills
            WHERE datetime(COALESCE(trade_time, created_at)) >= datetime(?)
              AND order_id IS NOT NULL
              AND order_id != ''
            """,
            (since_iso,),
        ).fetchone()[0]
        or 0
    )
    fills_linked_any = int(
        cur.execute(
            """
            SELECT COUNT(*)
            FROM edge_trade_fills f
            WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
              AND f.order_id IS NOT NULL
              AND f.order_id != ''
              AND EXISTS (SELECT 1 FROM auto_orders o WHERE o.order_id = f.order_id)
            """,
            (since_iso,),
        ).fetchone()[0]
        or 0
    )
    fills_linked_entry = int(
        cur.execute(
            """
            SELECT COUNT(*)
            FROM edge_trade_fills f
            WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
              AND f.order_id IS NOT NULL
              AND f.order_id != ''
              AND EXISTS (
                SELECT 1
                FROM auto_orders o
                WHERE o.order_id = f.order_id
                  AND o.action IN ('entry', 'entry_topup', 'entry_improve')
              )
            """,
            (since_iso,),
        ).fetchone()[0]
        or 0
    )
    fills_linked_exit = int(
        cur.execute(
            """
            SELECT COUNT(*)
            FROM edge_trade_fills f
            WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
              AND f.order_id IS NOT NULL
              AND f.order_id != ''
              AND EXISTS (
                SELECT 1
                FROM auto_orders o
                WHERE o.order_id = f.order_id
                  AND o.action IN ('exit', 'exit_reduce')
              )
            """,
            (since_iso,),
        ).fetchone()[0]
        or 0
    )
    payload: Dict[str, Any] = {
        "fills_total": fills_total,
        "fills_with_order_id": fills_with_order_id,
        "fills_with_auto_order": fills_linked_any,
        "fills_with_order_id_no_auto_order": max(0, fills_with_order_id - fills_linked_any),
        "fills_linked_auto_orders": fills_linked_any,
        "fills_linked_entry_orders": fills_linked_entry,
        "fills_linked_exit_orders": fills_linked_exit,
        "link_ratio_any": (
            float(fills_linked_any) / float(fills_with_order_id)
            if fills_with_order_id > 0
            else None
        ),
    }
    if _table_exists(conn, "trade_fill_attributions"):
        fills_with_attr = int(
            cur.execute(
                """
                SELECT COUNT(*)
                FROM edge_trade_fills f
                JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
                WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
                """,
                (since_iso,),
            ).fetchone()[0]
            or 0
        )
        fills_linked_strict = int(
            cur.execute(
                """
                SELECT COUNT(*)
                FROM edge_trade_fills f
                JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
                WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
                  AND a.linked = 1
                """,
                (since_iso,),
            ).fetchone()[0]
            or 0
        )
        fills_linked_strict_auto = int(
            cur.execute(
                """
                SELECT COUNT(*)
                FROM edge_trade_fills f
                JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
                JOIN auto_orders o ON o.order_id = f.order_id
                WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
                  AND a.linked = 1
                """,
                (since_iso,),
            ).fetchone()[0]
            or 0
        )
        fills_linked_strict_entry = int(
            cur.execute(
                """
                SELECT COUNT(*)
                FROM edge_trade_fills f
                JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
                JOIN auto_orders o ON o.order_id = f.order_id
                WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
                  AND a.linked = 1
                  AND o.action IN ('entry', 'entry_topup', 'entry_improve')
                """,
                (since_iso,),
            ).fetchone()[0]
            or 0
        )
        fills_linked_strict_exit = int(
            cur.execute(
                """
                SELECT COUNT(*)
                FROM edge_trade_fills f
                JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
                JOIN auto_orders o ON o.order_id = f.order_id
                WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
                  AND a.linked = 1
                  AND o.action IN ('exit', 'exit_reduce')
                """,
                (since_iso,),
            ).fetchone()[0]
            or 0
        )
        reason_rows = cur.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(a.reason), ''), 'unknown') AS reason, COUNT(*) AS n
            FROM edge_trade_fills f
            JOIN trade_fill_attributions a ON a.trade_id = f.trade_id
            WHERE datetime(COALESCE(f.trade_time, f.created_at)) >= datetime(?)
            GROUP BY reason
            ORDER BY n DESC, reason ASC
            LIMIT 10
            """,
            (since_iso,),
        ).fetchall()
        payload["fills_with_attribution"] = fills_with_attr
        payload["fills_linked_strict"] = fills_linked_strict
        payload["fills_linked_strict_auto_orders"] = fills_linked_strict_auto
        payload["fills_linked_strict_entry_orders"] = fills_linked_strict_entry
        payload["fills_linked_strict_exit_orders"] = fills_linked_strict_exit
        payload["link_ratio_strict"] = (
            float(fills_linked_strict) / float(fills_with_order_id)
            if fills_with_order_id > 0
            else None
        )
        payload["link_ratio_strict_of_auto_orders"] = (
            float(fills_linked_strict_auto) / float(fills_linked_any)
            if fills_linked_any > 0
            else None
        )
        payload["link_ratio_strict_of_entry_orders"] = (
            float(fills_linked_strict_entry) / float(fills_linked_entry)
            if fills_linked_entry > 0
            else None
        )
        payload["link_ratio_strict_of_exit_orders"] = (
            float(fills_linked_strict_exit) / float(fills_linked_exit)
            if fills_linked_exit > 0
            else None
        )
        payload["link_ratio_strict_of_attributed"] = (
            float(fills_linked_strict) / float(fills_with_attr)
            if fills_with_attr > 0
            else None
        )
        payload["attribution_reason_top"] = [
            {"reason": str(row[0] or ""), "count": int(row[1] or 0)}
            for row in reason_rows
        ]
    return payload


def _ece(samples: List[Tuple[float, int]], bins: int = 10) -> Optional[float]:
    if not samples:
        return None
    bucket: Dict[int, List[Tuple[float, int]]] = defaultdict(list)
    for p, y in samples:
        idx = min(bins - 1, max(0, int(_clamp01(p) * bins)))
        bucket[idx].append((p, y))
    n = len(samples)
    ece_val = 0.0
    for idx in range(bins):
        arr = bucket.get(idx) or []
        if not arr:
            continue
        conf = sum(x[0] for x in arr) / len(arr)
        acc = sum(x[1] for x in arr) / len(arr)
        ece_val += abs(conf - acc) * (len(arr) / n)
    return float(ece_val)


def _walkforward_eval_rows(
    rows: List[Dict[str, Any]],
    *,
    train_days: int,
    min_train: int,
    min_test: int,
) -> Dict[str, Any]:
    if not rows:
        return {"rows": 0, "daily": [], "overall": {}}
    prior_strength = _env_float("PM_EDGE_AUDIT_CALIB_PRIOR_STRENGTH", 10.0)
    if not math.isfinite(prior_strength) or prior_strength < 0:
        prior_strength = 10.0

    def _row_ts(r: Mapping[str, Any]) -> Optional[datetime]:
        raw = r.get("ts")
        if not raw:
            return None
        return _parse_iso(raw)
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    days = sorted(by_day.keys())
    daily = []
    joined_raw: List[Tuple[float, int]] = []
    joined_cal: List[Tuple[float, int]] = []
    for day in days:
        day_dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        train_start = (day_dt - timedelta(days=max(1, int(train_days)))).date().isoformat()
        train = []
        for d in days:
            if d >= day:
                break
            if d < train_start:
                continue
            train.extend(by_day[d])
        test = by_day[day]
        if len(train) < min_train or len(test) < min_test:
            continue
        bin_stats: Dict[int, List[int]] = defaultdict(list)
        for r in train:
            idx = min(9, max(0, int(r["p"] * 10)))
            bin_stats[idx].append(int(r["y"]))
        train_base = (sum(int(r["y"]) for r in train) + 1.0) / (len(train) + 2.0)
        bin_p: Dict[int, float] = {}
        for idx in range(10):
            ys = bin_stats.get(idx) or []
            if ys:
                bin_p[idx] = (sum(ys) + float(prior_strength) * float(train_base)) / (
                    len(ys) + float(prior_strength)
                )
            else:
                bin_p[idx] = float(train_base)
        raw_samples = [(float(r["p"]), int(r["y"])) for r in test]
        cal_samples = []
        for r in test:
            idx = min(9, max(0, int(r["p"] * 10)))
            cal_samples.append((float(bin_p[idx]), int(r["y"])))
        brier_raw = sum((p - y) ** 2 for p, y in raw_samples) / len(raw_samples)
        brier_cal = sum((p - y) ** 2 for p, y in cal_samples) / len(cal_samples)
        daily.append(
            {
                "day": day,
                "train_n": len(train),
                "test_n": len(test),
                "test_win_rate": sum(y for _, y in raw_samples) / len(raw_samples),
                "brier_raw": brier_raw,
                "brier_calibrated": brier_cal,
                "ece_raw": _ece(raw_samples),
                "ece_calibrated": _ece(cal_samples),
                "avg_real_edge": _avg([_safe_float(r["real_edge"]) for r in test]),
                "avg_pred_p_realize": _avg([_safe_float(r["p"]) for r in test]),
                "avg_pred_v2": _avg([_safe_float(r["v2"]) for r in test]),
            }
        )
        joined_raw.extend(raw_samples)
        joined_cal.extend(cal_samples)

    if not daily:
        # Fallback: if rows cluster into too few days, day-based walkforward yields
        # no evaluated days. Use a time-ordered split to still estimate calibration.
        ordered: List[Tuple[datetime, Dict[str, Any]]] = []
        for r in rows:
            ts = _row_ts(r)
            if ts is None:
                continue
            ordered.append((ts, r))
        if ordered:
            ordered.sort(key=lambda x: x[0])
            ordered_rows = [r for _, r in ordered]
        else:
            ordered_rows = list(rows)

        total_n = len(ordered_rows)
        if total_n >= (min_train + min_test):
            split = int(total_n * 0.8)
            split = max(min_train, min(split, total_n - min_test))
            train = ordered_rows[:split]
            test = ordered_rows[split:]
            if len(train) >= min_train and len(test) >= min_test:
                bin_stats: Dict[int, List[int]] = defaultdict(list)
                for r in train:
                    idx = min(9, max(0, int(r["p"] * 10)))
                    bin_stats[idx].append(int(r["y"]))
                train_base = (sum(int(r["y"]) for r in train) + 1.0) / (len(train) + 2.0)
                bin_p: Dict[int, float] = {}
                for idx in range(10):
                    ys = bin_stats.get(idx) or []
                    if ys:
                        bin_p[idx] = (sum(ys) + float(prior_strength) * float(train_base)) / (
                            len(ys) + float(prior_strength)
                        )
                    else:
                        bin_p[idx] = float(train_base)
                raw_samples = [(float(r["p"]), int(r["y"])) for r in test]
                cal_samples = []
                for r in test:
                    idx = min(9, max(0, int(r["p"] * 10)))
                    cal_samples.append((float(bin_p[idx]), int(r["y"])))
                brier_raw = sum((p - y) ** 2 for p, y in raw_samples) / len(raw_samples)
                brier_cal = sum((p - y) ** 2 for p, y in cal_samples) / len(cal_samples)
                stamp = (str(test[-1].get("day") or "").strip() or "time_split")
                daily.append(
                    {
                        "day": stamp,
                        "train_n": len(train),
                        "test_n": len(test),
                        "test_win_rate": sum(y for _, y in raw_samples) / len(raw_samples),
                        "brier_raw": brier_raw,
                        "brier_calibrated": brier_cal,
                        "ece_raw": _ece(raw_samples),
                        "ece_calibrated": _ece(cal_samples),
                        "avg_real_edge": _avg([_safe_float(r.get("real_edge")) for r in test]),
                        "avg_pred_p_realize": _avg([_safe_float(r.get("p")) for r in test]),
                        "avg_pred_v2": _avg([_safe_float(r.get("v2")) for r in test]),
                        "fallback_mode": "time_split",
                    }
                )
                joined_raw = list(raw_samples)
                joined_cal = list(cal_samples)

    rel_bins: Dict[int, List[Tuple[float, int]]] = defaultdict(list)
    for r in rows:
        idx = min(9, max(0, int(r["p"] * 10)))
        rel_bins[idx].append((float(r["p"]), int(r["y"])))
    reliability = []
    for idx in range(10):
        arr = rel_bins.get(idx) or []
        if not arr:
            continue
        reliability.append(
            {
                "bin": f"{idx/10:.1f}-{(idx+1)/10:.1f}",
                "n": len(arr),
                "avg_pred": sum(p for p, _ in arr) / len(arr),
                "obs_win_rate": sum(y for _, y in arr) / len(arr),
            }
        )

    return {
        "rows": len(rows),
        "days_total": len(days),
        "days_evaluated": len(daily),
        "daily": daily,
        "overall": {
            "test_rows": len(joined_raw),
            "brier_raw": (
                sum((p - y) ** 2 for p, y in joined_raw) / len(joined_raw)
                if joined_raw
                else None
            ),
            "brier_calibrated": (
                sum((p - y) ** 2 for p, y in joined_cal) / len(joined_cal)
                if joined_cal
                else None
            ),
            "ece_raw": _ece(joined_raw),
            "ece_calibrated": _ece(joined_cal),
            "reliability_bins": reliability,
        },
    }


def _walkforward_realized(
    audits: List[OrderAudit],
    *,
    train_days: int,
    min_train: int,
    min_test: int,
) -> Dict[str, Any]:
    rows = []
    for x in audits:
        if x.action != "entry":
            continue
        p = _clamp01(x.edge_realizable_p_realize)
        if p is None:
            continue
        if x.edge_trade_closed is not True:
            continue
        if x.edge_trade_realized_edge is None:
            continue
        y = 1 if x.edge_trade_realized_edge > 0 else 0
        rows.append(
            {
                "ts": x.created_at,
                "day": x.created_day,
                "p": p,
                "y": y,
                "v2": x.edge_realizable_v2,
                "real_edge": x.edge_trade_realized_edge,
            }
        )
    return _walkforward_eval_rows(
        rows,
        train_days=train_days,
        min_train=min_train,
        min_test=min_test,
    )


def _walkforward_proxy_from_decisions(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    horizon_hours: float,
    max_rows: int,
    train_days: int,
    min_train: int,
    min_test: int,
) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT slug, event_date, outcome, side, created_at, details_json
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND created_at >= ?
          AND json_extract(details_json, '$.exec_exit_price') IS NOT NULL
          AND json_extract(details_json, '$.exec_edge_exit') IS NOT NULL
          AND (
            json_extract(details_json, '$.edge_path_p_reach') IS NOT NULL
            OR json_extract(details_json, '$.edge_realizable_p_converge') IS NOT NULL
          )
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (since_iso, max_rows),
    )
    decision_rows = list(cur.fetchall())
    now_dt = datetime.now(timezone.utc)
    horizon_hours = max(0.5, float(horizon_hours))
    rows: List[Dict[str, Any]] = []
    p_source = "edge_path_p_reach"
    for drow in decision_rows:
        slug = str(drow["slug"] or "")
        event_date = str(drow["event_date"] or "")
        outcome = str(drow["outcome"] or "")
        side = str(drow["side"] or "").upper()
        created_at = str(drow["created_at"] or "")
        created_dt = _parse_iso(created_at)
        if not slug or not event_date or not outcome or side not in {"YES", "NO"} or created_dt is None:
            continue
        event_end_dt = _event_end_utc(event_date)
        if event_end_dt is None:
            continue
        # We want a fast, fully-observed proxy (so we can audit even when most markets
        # haven't settled yet). Label "y" as whether the *target exit* was touched within
        # a short horizon, but scale "p" down from the full-horizon probability using a
        # simple hazard-rate assumption so p/y semantics stay aligned.
        horizon_end_dt = created_dt + timedelta(hours=horizon_hours)
        window_end_dt = horizon_end_dt if horizon_end_dt < event_end_dt else event_end_dt
        if window_end_dt > now_dt:
            # Only score fully-observed windows. Otherwise "not yet touched" becomes a false negative.
            continue
        try:
            details = json.loads(drow["details_json"] or "{}")
        except Exception:
            details = {}
        if not isinstance(details, dict):
            continue
        p_full = _clamp01(_safe_float(details.get("edge_path_p_reach")))
        base_source = "edge_path_p_reach"
        if p_full is None:
            p_full = _clamp01(_safe_float(details.get("edge_realizable_p_converge")))
            if p_full is not None:
                base_source = "edge_realizable_p_converge"
        if p_full is None:
            continue

        target = _safe_float(details.get("edge_realizable_target_exit_price"))
        if target is None or not math.isfinite(target) or target <= 0:
            tick = _tick_before(
                conn,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                side=side,
                ts=created_at,
            )
            exec_exit_price = _safe_float(details.get("exec_exit_price"))
            exec_edge_exit = _safe_float(details.get("exec_edge_exit"))
            base_bid = _safe_float(tick["best_bid"]) if tick is not None else None
            spread_val = _safe_float(tick["spread"]) if tick is not None else _safe_float(details.get("spread"))
            target = _derive_target_exit_price(
                exec_exit_price=exec_exit_price,
                exec_edge_exit=exec_edge_exit,
                base_bid=base_bid,
                spread=spread_val,
            )
        if target is None or target <= 0:
            continue
        remaining_hours = max(0.0, (event_end_dt - created_dt).total_seconds() / 3600.0)
        eval_hours = max(0.0, (window_end_dt - created_dt).total_seconds() / 3600.0)
        if remaining_hours <= 1e-6 or eval_hours >= remaining_hours * 0.999:
            p = float(p_full)
            p_source = base_source
        else:
            # Convert a full-horizon reach probability into a shorter-horizon probability.
            # If "reach" is approximately a memoryless (Poisson) event over time, then:
            #   p(h) = 1 - (1 - p_full)^(h / H_full)
            try:
                p = 1.0 - (1.0 - float(p_full)) ** (float(eval_hours) / float(remaining_hours))
            except Exception:
                p = float(p_full)
            p = _clamp01(p)
            p_source = f"{base_source}_scaled_hazard"
        touched = _touched_between(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            side=side,
            start_ts=created_at,
            end_ts=window_end_dt.isoformat(timespec="seconds"),
            action="exit",
            limit_price=target,
        )
        if touched is None:
            continue
        rows.append(
            {
                "ts": created_at,
                "day": created_dt.date().isoformat(),
                "p": p,
                "y": 1 if touched else 0,
                "v2": _safe_float(details.get("edge_realizable_v2")),
                "real_edge": None,
            }
        )
    result = _walkforward_eval_rows(
        rows,
        train_days=train_days,
        min_train=min_train,
        min_test=min_test,
    )
    result["proxy_label"] = f"target_touched_within_{float(horizon_hours):.1f}h_or_event_end"
    result["proxy_p_source"] = p_source
    result["decision_rows_scanned"] = len(decision_rows)
    return result


def _build_gate_report(
    *,
    replay: Dict[str, Any],
    walk_realized: Dict[str, Any],
    walk_proxy: Dict[str, Any],
    min_entry_orders: int,
    min_link_ratio: float,
    max_tick_age_p90_sec: float,
    min_proxy_rows: int,
    min_proxy_days: int,
    max_proxy_ece_cal: float,
    max_proxy_brier_cal: float,
    min_realized_rows: int,
    max_realized_ece_cal: float,
    max_realized_brier_cal: float,
    require_realized: bool,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def _append_check(
        *,
        name: str,
        value: Optional[float],
        threshold: float,
        comparator: str,
        enforced: bool = True,
        note: str = "",
    ) -> None:
        status = "skip"
        ok: Optional[bool] = None
        if enforced:
            if value is None:
                ok = False
                status = "fail"
            elif comparator == ">=":
                ok = bool(value >= threshold)
                status = "pass" if ok else "fail"
            elif comparator == "<=":
                ok = bool(value <= threshold)
                status = "pass" if ok else "fail"
            else:
                ok = False
                status = "fail"
        checks.append(
            {
                "name": name,
                "status": status,
                "ok": ok,
                "enforced": bool(enforced),
                "value": value,
                "comparator": comparator,
                "threshold": threshold,
                "note": note,
            }
        )

    entry_orders = _safe_float(replay.get("entry_orders"))
    fills_linkage = replay.get("fills_linkage") if isinstance(replay.get("fills_linkage"), dict) else {}
    link_ratio_strict = _safe_float(fills_linkage.get("link_ratio_strict"))
    link_ratio_strict_auto = _safe_float(fills_linkage.get("link_ratio_strict_of_auto_orders"))
    link_ratio_any = _safe_float(fills_linkage.get("link_ratio_any"))
    link_ratio_gate = link_ratio_strict_auto
    link_ratio_source = "link_ratio_strict_of_auto_orders"
    if link_ratio_gate is None:
        link_ratio_gate = link_ratio_strict
        link_ratio_source = "link_ratio_strict"
    if link_ratio_gate is None:
        link_ratio_gate = link_ratio_any
        link_ratio_source = "link_ratio_any"
    tick_age = replay.get("tick_age_sec") if isinstance(replay.get("tick_age_sec"), dict) else {}
    tick_age_p90 = _safe_float(tick_age.get("p90"))
    proxy_overall = walk_proxy.get("overall") if isinstance(walk_proxy.get("overall"), dict) else {}
    proxy_rows = _safe_float(walk_proxy.get("rows"))
    proxy_days = _safe_float(walk_proxy.get("days_evaluated"))
    proxy_ece_cal = _safe_float(proxy_overall.get("ece_calibrated"))
    proxy_brier_cal = _safe_float(proxy_overall.get("brier_calibrated"))
    realized_overall = walk_realized.get("overall") if isinstance(walk_realized.get("overall"), dict) else {}
    realized_rows = _safe_float(walk_realized.get("rows"))
    realized_ece_cal = _safe_float(realized_overall.get("ece_calibrated"))
    realized_brier_cal = _safe_float(realized_overall.get("brier_calibrated"))

    _append_check(
        name="entry_orders_min",
        value=entry_orders,
        comparator=">=",
        threshold=float(min_entry_orders),
        enforced=True,
    )
    _append_check(
        name="fill_link_ratio_min",
        value=link_ratio_gate,
        comparator=">=",
        threshold=float(min_link_ratio),
        enforced=True,
        note=f"source={link_ratio_source}",
    )
    _append_check(
        name="fill_link_ratio_all_order_ids_info",
        value=link_ratio_strict,
        comparator=">=",
        threshold=float(min_link_ratio),
        enforced=False,
        note="informational: strict linked / fills_with_order_id",
    )
    _append_check(
        name="tick_age_p90_max",
        value=tick_age_p90,
        comparator="<=",
        threshold=float(max_tick_age_p90_sec),
        enforced=True,
    )
    _append_check(
        name="walk_proxy_rows_min",
        value=proxy_rows,
        comparator=">=",
        threshold=float(min_proxy_rows),
        enforced=True,
    )
    _append_check(
        name="walk_proxy_days_min",
        value=proxy_days,
        comparator=">=",
        threshold=float(min_proxy_days),
        enforced=True,
    )
    _append_check(
        name="walk_proxy_ece_cal_max",
        value=proxy_ece_cal,
        comparator="<=",
        threshold=float(max_proxy_ece_cal),
        enforced=True,
    )
    _append_check(
        name="walk_proxy_brier_cal_max",
        value=proxy_brier_cal,
        comparator="<=",
        threshold=float(max_proxy_brier_cal),
        enforced=True,
    )

    realized_enforced = bool(require_realized) or (
        realized_rows is not None and realized_rows >= float(min_realized_rows)
    )
    realized_note = (
        ""
        if realized_enforced
        else f"realized_rows<{int(min_realized_rows)} and gate_require_realized=0"
    )
    _append_check(
        name="walk_realized_rows_min",
        value=realized_rows,
        comparator=">=",
        threshold=float(min_realized_rows),
        enforced=bool(require_realized),
        note=("required" if require_realized else "informational"),
    )
    _append_check(
        name="walk_realized_ece_cal_max",
        value=realized_ece_cal,
        comparator="<=",
        threshold=float(max_realized_ece_cal),
        enforced=realized_enforced,
        note=realized_note,
    )
    _append_check(
        name="walk_realized_brier_cal_max",
        value=realized_brier_cal,
        comparator="<=",
        threshold=float(max_realized_brier_cal),
        enforced=realized_enforced,
        note=realized_note,
    )

    enforced_checks = [c for c in checks if c.get("enforced")]
    failed_checks = [c for c in enforced_checks if c.get("ok") is not True]
    passed_checks = [c for c in enforced_checks if c.get("ok") is True]
    return {
        "pass": len(failed_checks) == 0,
        "fill_link_ratio_source": link_ratio_source,
        "checks_total": len(checks),
        "checks_enforced": len(enforced_checks),
        "checks_passed": len(passed_checks),
        "checks_failed": len(failed_checks),
        "failed_names": [str(c.get("name") or "") for c in failed_checks],
        "checks": checks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Executable edge audit: replay + walk-forward calibration.")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--since-days", type=int, default=60)
    ap.add_argument("--touch-horizon-hours", type=float, default=2.0)
    ap.add_argument("--walk-train-days", type=int, default=30)
    ap.add_argument("--walk-min-train", type=int, default=30)
    ap.add_argument("--walk-min-test", type=int, default=5)
    ap.add_argument(
        "--walk-proxy-horizon-hours",
        type=float,
        default=_env_float("PM_EDGE_AUDIT_WALK_PROXY_HORIZON_HOURS", 12.0),
    )
    ap.add_argument(
        "--walk-proxy-max-rows",
        type=int,
        default=_env_int("PM_EDGE_AUDIT_WALK_PROXY_MAX_ROWS", 60000),
    )
    ap.add_argument(
        "--mtm-mature-hours",
        type=float,
        default=_env_float("PM_EDGE_EXEC_MTM_MATURE_HOURS", 8.0),
    )
    ap.add_argument("--max-records", type=int, default=400)
    ap.add_argument("--enforce-gate", default="0")
    ap.add_argument("--gate-min-entry-orders", type=int, default=_env_int("PM_EDGE_AUDIT_GATE_MIN_ENTRY_ORDERS", 80))
    ap.add_argument(
        "--gate-min-link-ratio",
        type=float,
        default=_env_float("PM_EDGE_AUDIT_GATE_MIN_LINK_RATIO", 0.65),
    )
    ap.add_argument(
        "--gate-max-tick-age-p90-sec",
        type=float,
        default=_env_float("PM_EDGE_AUDIT_GATE_MAX_TICK_AGE_P90_SEC", 14400.0),
    )
    ap.add_argument("--gate-min-proxy-rows", type=int, default=_env_int("PM_EDGE_AUDIT_GATE_MIN_PROXY_ROWS", 120))
    ap.add_argument("--gate-min-proxy-days", type=int, default=_env_int("PM_EDGE_AUDIT_GATE_MIN_PROXY_DAYS", 1))
    ap.add_argument("--gate-max-proxy-ece-cal", type=float, default=_env_float("PM_EDGE_AUDIT_GATE_MAX_PROXY_ECE_CAL", 0.12))
    ap.add_argument("--gate-max-proxy-brier-cal", type=float, default=_env_float("PM_EDGE_AUDIT_GATE_MAX_PROXY_BRIER_CAL", 0.24))
    ap.add_argument("--gate-min-realized-rows", type=int, default=_env_int("PM_EDGE_AUDIT_GATE_MIN_REALIZED_ROWS", 30))
    ap.add_argument("--gate-max-realized-ece-cal", type=float, default=_env_float("PM_EDGE_AUDIT_GATE_MAX_REALIZED_ECE_CAL", 0.12))
    ap.add_argument("--gate-max-realized-brier-cal", type=float, default=_env_float("PM_EDGE_AUDIT_GATE_MAX_REALIZED_BRIER_CAL", 0.24))
    ap.add_argument("--gate-require-realized", default="0")
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "data" / "reports" / "edge_executable_audit.json"),
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(args.since_days)))
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        audits = _build_order_audits(
            conn,
            since_dt=since_dt,
            touch_horizon_hours=float(args.touch_horizon_hours),
        )

    replay = _replay_summary(
        audits,
        mtm_mature_hours=max(0.0, float(args.mtm_mature_hours)),
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        replay["fills_linkage"] = _fills_linkage_summary(
            conn,
            since_iso=since_dt.isoformat(timespec="seconds"),
        )
    walk_realized = _walkforward_realized(
        audits,
        train_days=max(7, int(args.walk_train_days)),
        min_train=max(5, int(args.walk_min_train)),
        min_test=max(1, int(args.walk_min_test)),
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        walk_proxy = _walkforward_proxy_from_decisions(
            conn,
            since_iso=since_dt.isoformat(timespec="seconds"),
            horizon_hours=max(0.5, float(args.walk_proxy_horizon_hours)),
            max_rows=max(200, int(args.walk_proxy_max_rows)),
            train_days=max(7, int(args.walk_train_days)),
            min_train=max(5, int(args.walk_min_train)),
            min_test=max(1, int(args.walk_min_test)),
        )
    gate_require_realized = _truthy_flag(args.gate_require_realized)
    gate_report = _build_gate_report(
        replay=replay,
        walk_realized=walk_realized,
        walk_proxy=walk_proxy,
        min_entry_orders=max(1, int(args.gate_min_entry_orders)),
        min_link_ratio=max(0.0, min(1.0, float(args.gate_min_link_ratio))),
        max_tick_age_p90_sec=max(1.0, float(args.gate_max_tick_age_p90_sec)),
        min_proxy_rows=max(1, int(args.gate_min_proxy_rows)),
        min_proxy_days=max(1, int(args.gate_min_proxy_days)),
        max_proxy_ece_cal=max(0.0, min(1.0, float(args.gate_max_proxy_ece_cal))),
        max_proxy_brier_cal=max(0.0, min(1.0, float(args.gate_max_proxy_brier_cal))),
        min_realized_rows=max(1, int(args.gate_min_realized_rows)),
        max_realized_ece_cal=max(0.0, min(1.0, float(args.gate_max_realized_ece_cal))),
        max_realized_brier_cal=max(0.0, min(1.0, float(args.gate_max_realized_brier_cal))),
        require_realized=gate_require_realized,
    )
    enforce_gate = _truthy_flag(args.enforce_gate)
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_dt.isoformat(timespec="seconds"),
        "config": {
            "db": str(db_path),
            "since_days": int(args.since_days),
            "touch_horizon_hours": float(args.touch_horizon_hours),
            "walk_train_days": int(args.walk_train_days),
            "walk_min_train": int(args.walk_min_train),
            "walk_min_test": int(args.walk_min_test),
            "walk_proxy_horizon_hours": float(args.walk_proxy_horizon_hours),
            "walk_proxy_max_rows": int(args.walk_proxy_max_rows),
            "max_records": int(args.max_records),
            "enforce_gate": enforce_gate,
            "gate_require_realized": gate_require_realized,
            "gate_thresholds": {
                "min_entry_orders": max(1, int(args.gate_min_entry_orders)),
                "min_link_ratio": max(0.0, min(1.0, float(args.gate_min_link_ratio))),
                "max_tick_age_p90_sec": max(1.0, float(args.gate_max_tick_age_p90_sec)),
                "min_proxy_rows": max(1, int(args.gate_min_proxy_rows)),
                "min_proxy_days": max(1, int(args.gate_min_proxy_days)),
                "max_proxy_ece_cal": max(0.0, min(1.0, float(args.gate_max_proxy_ece_cal))),
                "max_proxy_brier_cal": max(0.0, min(1.0, float(args.gate_max_proxy_brier_cal))),
                "min_realized_rows": max(1, int(args.gate_min_realized_rows)),
                "max_realized_ece_cal": max(0.0, min(1.0, float(args.gate_max_realized_ece_cal))),
                "max_realized_brier_cal": max(0.0, min(1.0, float(args.gate_max_realized_brier_cal))),
            },
        },
        "replay": replay,
        "walkforward_realized": walk_realized,
        "walkforward_proxy": walk_proxy,
        "gate": gate_report,
        "records_sample": [x.as_dict() for x in audits[-max(1, int(args.max_records)) :]],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    exit_code = 0 if (not enforce_gate or bool(gate_report.get("pass"))) else 2
    print(
        json.dumps(
            {
                "ok": exit_code == 0,
                "out": str(out_path),
                "orders": len(audits),
                "replay_entry_orders": replay.get("entry_orders"),
                "walk_realized_rows": walk_realized.get("rows"),
                "walk_proxy_rows": walk_proxy.get("rows"),
                "walk_proxy_days_evaluated": walk_proxy.get("days_evaluated"),
                "gate_pass": bool(gate_report.get("pass")),
                "gate_failed_names": gate_report.get("failed_names", []),
                "enforce_gate": enforce_gate,
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
