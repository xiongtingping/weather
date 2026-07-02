#!/usr/bin/env python3
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from edge_realizable import edge_realizable_bucket_key, _default_bins

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


def _target_exit_price_clamped(
    *,
    exec_exit_price: Optional[float],
    exec_edge_exit: Optional[float],
    base_bid: Optional[float],
    spread: Optional[float],
    headroom_min: float,
) -> Optional[float]:
    if exec_exit_price is None or not math.isfinite(exec_exit_price):
        return None
    if exec_edge_exit is None or not math.isfinite(exec_edge_exit):
        return None
    raw_target = float(exec_exit_price) + float(exec_edge_exit) - float(headroom_min)
    if not math.isfinite(raw_target) or raw_target <= 0:
        return None
    if base_bid is None or not math.isfinite(base_bid):
        return raw_target
    delta_raw = raw_target - float(base_bid)
    if not math.isfinite(delta_raw):
        return None
    min_delta = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MIN_DELTA", 0.0)
    if min_delta < 0:
        min_delta = 0.0
    max_delta = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MAX_DELTA", 0.06)
    if max_delta <= 0:
        max_delta = 0.06
    spread_mult = _env_float("PM_EDGE_REALIZABLE_TARGET_EXIT_MAX_SPREAD_MULT", 2.0)
    if spread_mult <= 0:
        spread_mult = 2.0
    cap_candidates: List[float] = [float(max_delta)]
    if spread is not None and math.isfinite(spread) and spread > 0:
        cap_candidates.append(float(spread) * float(spread_mult))
    delta_cap = min(cap_candidates) if cap_candidates else None
    delta_final = float(delta_raw)
    if delta_final < float(min_delta):
        delta_final = float(min_delta)
    if delta_cap is not None and math.isfinite(delta_cap) and delta_final > float(delta_cap):
        delta_final = float(delta_cap)
    if delta_final < 0:
        delta_final = 0.0
    target = float(base_bid) + float(delta_final)
    if not math.isfinite(target) or target <= 0:
        return None
    return float(target)


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


def _side_value(item: Dict[str, Any], base: str, side: str) -> Any:
    if not isinstance(item, dict):
        return None
    side_key = str(side or "").strip().upper()
    if side_key == "YES":
        if base in item:
            return item.get(base)
        return item.get(f"{base}_yes")
    if side_key == "NO":
        if f"{base}_no" in item:
            return item.get(f"{base}_no")
        return item.get(base)
    return item.get(base)


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


class _TickWindowCache:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cache: Dict[Tuple[str, str, str, str, str, str], Optional[Tuple[Optional[float], Optional[float]]]] = {}
        self._preloaded_market_windows: set[Tuple[str, str, str, str]] = set()
        self.stats: Dict[str, int] = {
            "tick_windows_requested": 0,
            "tick_windows_preloaded": 0,
            "tick_windows_skipped": 0,
            "tick_preload_batches": 0,
            "tick_preload_batches_skipped": 0,
            "tick_preload_timeouts": 0,
        }

    @staticmethod
    def _key(slug: str, event_date: str, outcome: str, side: str, t0: datetime, t1: datetime) -> Tuple[str, str, str, str, str, str]:
        return (
            str(slug or ""),
            str(event_date or ""),
            str(outcome or ""),
            str(side or "").upper(),
            t0.isoformat(timespec="seconds"),
            t1.isoformat(timespec="seconds"),
        )

    @staticmethod
    def _market_window_key(slug: str, event_date: str, t0: datetime, t1: datetime) -> Tuple[str, str, str, str]:
        return (
            str(slug or ""),
            str(event_date or ""),
            t0.isoformat(timespec="seconds"),
            t1.isoformat(timespec="seconds"),
        )

    def preload_market(self, slug: str, event_date: str, t0: datetime, t1: datetime) -> None:
        window_key = self._market_window_key(slug, event_date, t0, t1)
        if window_key in self._preloaded_market_windows:
            return
        self._preloaded_market_windows.add(window_key)
        try:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT outcome, side, MAX(best_bid), MIN(best_ask)
                FROM event_market_ticks
                WHERE slug = ? AND event_date = ?
                  AND created_at >= ? AND created_at <= ?
                GROUP BY outcome, side
                """,
                window_key,
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            outcome = str(row[0] or "")
            side = str(row[1] or "").upper()
            if not outcome or not side:
                continue
            self._cache[(window_key[0], window_key[1], outcome, side, window_key[2], window_key[3])] = (
                _safe_float(row[2]),
                _safe_float(row[3]),
            )

    def preload_windows(
        self,
        windows: List[Tuple[str, str, datetime, datetime]],
        *,
        batch_size: int = 500,
        timeout_sec: float = 20.0,
    ) -> None:
        pending: List[Tuple[int, Tuple[str, str, str, str]]] = []
        for slug, event_date, t0, t1 in windows:
            window_key = self._market_window_key(slug, event_date, t0, t1)
            if window_key in self._preloaded_market_windows:
                continue
            pending.append((len(pending), window_key))
        if not pending:
            return
        self.stats["tick_windows_requested"] += len(pending)
        chunk_size = max(1, int(batch_size or 500))
        for start in range(0, len(pending), chunk_size):
            self._preload_window_batch(pending[start : start + chunk_size], timeout_sec=timeout_sec)

    def _preload_window_batch(
        self,
        pending: List[Tuple[int, Tuple[str, str, str, str]]],
        *,
        timeout_sec: float = 20.0,
    ) -> None:
        if not pending:
            return
        self.stats["tick_preload_batches"] += 1
        deadline = None
        if timeout_sec is not None and math.isfinite(float(timeout_sec)) and float(timeout_sec) > 0:
            deadline = time.monotonic() + float(timeout_sec)

        def _abort_if_timed_out() -> int:
            if deadline is not None and time.monotonic() > deadline:
                return 1
            return 0

        try:
            cur = self.conn.cursor()
            if deadline is not None:
                self.conn.set_progress_handler(_abort_if_timed_out, 10000)
            cur.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS _edge_calib_tick_windows (
                    window_id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    start_ts TEXT NOT NULL,
                    end_ts TEXT NOT NULL
                )
                """
            )
            cur.execute("DELETE FROM _edge_calib_tick_windows")
            cur.executemany(
                """
                INSERT INTO _edge_calib_tick_windows(window_id, slug, event_date, start_ts, end_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(idx, key[0], key[1], key[2], key[3]) for idx, key in pending],
            )
            cur.execute(
                """
                SELECT w.window_id, t.outcome, UPPER(t.side), MAX(t.best_bid), MIN(t.best_ask)
                FROM _edge_calib_tick_windows w
                JOIN event_market_ticks t
                  ON t.slug = w.slug
                 AND t.event_date = w.event_date
                 AND t.created_at >= w.start_ts
                 AND t.created_at <= w.end_ts
                GROUP BY w.window_id, t.outcome, UPPER(t.side)
                """
            )
            rows = cur.fetchall()
            if deadline is not None and time.monotonic() > deadline:
                raise sqlite3.OperationalError("tick_preload_timeout")
        except sqlite3.OperationalError:
            self.stats["tick_preload_batches_skipped"] += 1
            if deadline is not None and time.monotonic() > deadline:
                self.stats["tick_preload_timeouts"] += 1
            for _, key in pending:
                self._preloaded_market_windows.add(key)
            self.stats["tick_windows_skipped"] += len(pending)
            return
        finally:
            if deadline is not None:
                self.conn.set_progress_handler(None, 0)
        keys_by_id = {idx: key for idx, key in pending}
        for _, key in pending:
            self._preloaded_market_windows.add(key)
        self.stats["tick_windows_preloaded"] += len(pending)
        for row in rows:
            window_key = keys_by_id.get(int(row[0]))
            if window_key is None:
                continue
            outcome = str(row[1] or "")
            side = str(row[2] or "").upper()
            if not outcome or not side:
                continue
            self._cache[(window_key[0], window_key[1], outcome, side, window_key[2], window_key[3])] = (
                _safe_float(row[3]),
                _safe_float(row[4]),
            )

    def get(
        self,
        slug: str,
        event_date: str,
        outcome: str,
        side: str,
        t0: datetime,
        t1: datetime,
    ) -> Optional[Tuple[Optional[float], Optional[float]]]:
        key = self._key(slug, event_date, outcome, side, t0, t1)
        if key in self._cache:
            return self._cache[key]
        if self._market_window_key(slug, event_date, t0, t1) in self._preloaded_market_windows:
            return None
        try:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT MAX(best_bid), MIN(best_ask)
                FROM event_market_ticks
                WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
                  AND created_at >= ? AND created_at <= ?
                """,
                key,
            )
            row = cur.fetchone()
        except sqlite3.OperationalError:
            self._cache[key] = None
            return None
        if not row:
            self._cache[key] = None
            return None
        value = (_safe_float(row[0]), _safe_float(row[1]))
        self._cache[key] = value
        return value


def _touches_target(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    t0: datetime,
    t1: datetime,
    target: float,
    *,
    window_cache: Optional[_TickWindowCache] = None,
) -> Optional[bool]:
    if not slug or not event_date or not outcome or not side:
        return None
    if window_cache is not None:
        stats = window_cache.get(slug, event_date, outcome, side, t0, t1)
        if stats is None:
            return None
        best = stats[0]
        if best is None:
            return None
        return bool(best >= target - 1e-9)
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


def _touches_entry_limit(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    t0: datetime,
    t1: datetime,
    limit_price: float,
    *,
    window_cache: Optional[_TickWindowCache] = None,
) -> Optional[bool]:
    if limit_price is None:
        return None
    if not slug or not event_date or not outcome or not side:
        return None
    if window_cache is not None:
        stats = window_cache.get(slug, event_date, outcome, side, t0, t1)
        if stats is None:
            return None
        best = stats[1]
        if best is None:
            return None
        return bool(best <= limit_price + 1e-9)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MIN(best_ask)
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
    return bool(best <= limit_price + 1e-9)


def _nearest_tick_meta(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    ts: datetime,
    *,
    depth_side: str = "ask",
) -> Tuple[Optional[float], Optional[float]]:
    depth_col = "ask_depth_topN"
    if str(depth_side or "").strip().lower() in {"bid", "b"}:
        depth_col = "bid_depth_topN"
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT spread, {depth_col}
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


def _iter_exit_orders(conn: sqlite3.Connection, since_iso: str) -> Iterable[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT order_id, slug, event_date, outcome, side, price, size, order_type, created_at
        FROM auto_orders
        WHERE action = 'exit' AND created_at >= ?
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


def _exit_filled_set(conn: sqlite3.Connection, since_iso: str) -> set:
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT order_id
            FROM edge_trade_fills
            WHERE trade_time >= ?
              AND order_id IS NOT NULL AND order_id != ''
              AND UPPER(COALESCE(trade_side, '')) = 'SELL'
            """,
            (since_iso,),
        )
        return {str(r[0]) for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def _latest_fact_as_of(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            """
            SELECT fact_as_of
            FROM event_fact_history
            WHERE fact_as_of IS NOT NULL AND fact_as_of != ''
            ORDER BY fact_as_of DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return str(row[0] or "").strip() or None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--since-days", type=int, default=14)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "edge_realizable_calibration.candidate.json"))
    ap.add_argument("--max-facts", type=int, default=3000)
    ap.add_argument("--min-hours", type=float, default=0.5)
    ap.add_argument("--max-hours", type=float, default=12.0)
    ap.add_argument("--ratio", type=float, default=0.25)
    ap.add_argument("--fill-mode", choices=["orders", "ticks"], default="orders")
    ap.add_argument("--tick-window-batch-size", type=int, default=250)
    ap.add_argument("--tick-window-timeout-sec", type=float, default=20.0)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")

    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(args.since_days)))
    since_iso = since_dt.isoformat(timespec="seconds")

    bins = _default_bins()
    converge_stats: Dict[str, Dict[str, int]] = {}
    entry_stats: Dict[str, Dict[str, int]] = {}
    exit_stats: Dict[str, Dict[str, int]] = {}
    exit_attempts = 0
    exit_filled = 0

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        sample_since_iso = since_iso
        cur.execute(
            """
            SELECT slug, event_date, fact_as_of, fact_json
            FROM event_fact_history
            WHERE fact_as_of >= ?
            ORDER BY fact_as_of DESC
            LIMIT ?
            """,
            (sample_since_iso, int(args.max_facts)),
        )
        rows = cur.fetchall()
        if not rows:
            latest_fact_as_of = _latest_fact_as_of(conn)
            sample_cutoff = _parse_iso(latest_fact_as_of) if latest_fact_as_of else None
            if sample_cutoff is not None:
                sample_since_iso = (
                    sample_cutoff - timedelta(days=max(1, int(args.since_days)))
                ).isoformat(timespec="seconds")
                cur.execute(
                    """
                    SELECT slug, event_date, fact_as_of, fact_json
                    FROM event_fact_history
                    WHERE fact_as_of >= ?
                    ORDER BY fact_as_of DESC
                    LIMIT ?
                    """,
                    (sample_since_iso, int(args.max_facts)),
                )
                rows = cur.fetchall()
        tick_windows: List[Tuple[str, str, datetime, datetime]] = []
        if args.fill_mode == "ticks":
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
                tick_windows.append((slug, event_date, fact_dt, t1))
        tick_window_cache = _TickWindowCache(conn) if args.fill_mode == "ticks" else None
        if tick_window_cache is not None:
            tick_window_cache.preload_windows(
                tick_windows,
                batch_size=args.tick_window_batch_size,
                timeout_sec=args.tick_window_timeout_sec,
            )
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
                for side in ("YES", "NO"):
                    q_entry = _safe_float(_side_value(item, "q_entry", side))
                    best_bid = _safe_float(_side_value(item, "best_bid", side))
                    best_ask = _safe_float(_side_value(item, "best_ask", side))
                    spread = _safe_float(_side_value(item, "spread", side))
                    depth_total = _safe_float(_side_value(item, "ask_depth_topN", side))
                    entry_size = _safe_float(_side_value(item, "entry_size", side))
                    exec_edge_entry = _safe_float(_side_value(item, "exec_edge_entry", side))
                    exec_exit_price = _safe_float(_side_value(item, "exec_exit_price", side))
                    exec_edge_exit = _safe_float(_side_value(item, "exec_edge_exit", side))
                    target_exit_price = _target_exit_price_clamped(
                        exec_exit_price=exec_exit_price,
                        exec_edge_exit=exec_edge_exit,
                        base_bid=best_bid,
                        spread=spread,
                        headroom_min=0.02,
                    )
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
                    if depth_ratio is None:
                        depth_ratio = _safe_float(_side_value(item, "depth_ratio", side))
                    if args.fill_mode == "ticks":
                        hit_exit = None
                        key_entry = edge_realizable_bucket_key(
                            days_to_settle=dts,
                            spread=spread,
                            depth_ratio=depth_ratio,
                            edge_val=None,
                            delta_exit=None,
                            order_type=None,
                            bins=bins,
                        )
                        entry_price = _safe_float(_side_value(item, "exec_entry_price", side))
                        if entry_price is None:
                            entry_price = _safe_float(_side_value(item, "entry_price", side))
                        if entry_price is not None:
                            hit_entry = _touches_entry_limit(
                                conn,
                                slug,
                                event_date,
                                outcome,
                                side,
                                fact_dt,
                                t1,
                                entry_price,
                                window_cache=tick_window_cache,
                            )
                            if hit_entry is not None:
                                stat_entry = entry_stats.setdefault(key_entry, {"filled": 0, "total": 0})
                                stat_entry["total"] += 1
                                if hit_entry:
                                    stat_entry["filled"] += 1
                        if target_exit_price is not None:
                            hit_exit = _touches_target(
                                conn,
                                slug,
                                event_date,
                                outcome,
                                side,
                                fact_dt,
                                t1,
                                target_exit_price,
                                window_cache=tick_window_cache,
                            )
                            if hit_exit is not None:
                                stat_exit = exit_stats.setdefault(key_entry, {"filled": 0, "total": 0})
                                stat_exit["total"] += 1
                                exit_attempts += 1
                                if hit_exit:
                                    stat_exit["filled"] += 1
                                    exit_filled += 1

                    if exec_edge_entry is None or best_bid is None or best_ask is None:
                        continue
                    if target_exit_price is None:
                        continue
                    delta_exit = target_exit_price - best_bid
                    key = edge_realizable_bucket_key(
                        days_to_settle=dts,
                        spread=spread,
                        depth_ratio=depth_ratio,
                        edge_val=None,
                        delta_exit=delta_exit,
                        order_type=None,
                        bins=bins,
                    )
                    if args.fill_mode == "ticks" and hit_exit is not None:
                        hit = hit_exit
                    else:
                        hit = _touches_target(
                            conn,
                            slug,
                            event_date,
                            outcome,
                            side,
                            fact_dt,
                            t1,
                            target_exit_price,
                            window_cache=tick_window_cache,
                        )
                    if hit is None:
                        continue
                    stat = converge_stats.setdefault(key, {"hit": 0, "total": 0})
                    stat["total"] += 1
                    if hit:
                        stat["hit"] += 1

        if args.fill_mode == "orders":
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

            exit_order_ids: set = set()
            exit_filled_orders = _exit_filled_set(conn, since_iso)
            for order in _iter_exit_orders(conn, since_iso):
                slug = order["slug"]
                event_date = order["event_date"]
                outcome = order["outcome"]
                side = order["side"]
                created_at = order["created_at"]
                order_dt = _parse_iso(created_at)
                if not slug or not event_date or not outcome or side not in {"YES", "NO"} or order_dt is None:
                    continue
                exit_order_ids.add(order["order_id"])
                spread, depth_top = _nearest_tick_meta(
                    conn,
                    slug,
                    event_date,
                    outcome,
                    side,
                    order_dt,
                    depth_side="bid",
                )
                size = order.get("size")
                depth_ratio = None
                if depth_top is not None and math.isfinite(depth_top) and size is not None and math.isfinite(size) and size > 0:
                    depth_ratio = depth_top / size
                dts_exit = _days_to_settle(event_date, created_at)
                key = edge_realizable_bucket_key(
                    days_to_settle=dts_exit,
                    spread=spread,
                    depth_ratio=depth_ratio,
                    edge_val=None,
                    delta_exit=None,
                    order_type=order.get("order_type"),
                    bins=bins,
                )
                stat = exit_stats.setdefault(key, {"filled": 0, "total": 0})
                stat["total"] += 1
                if order["order_id"] and order["order_id"] in exit_filled_orders:
                    stat["filled"] += 1
            exit_attempts = len(exit_order_ids)
            exit_filled = len(exit_order_ids.intersection(exit_filled_orders))

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

    p_fill_exit_buckets = {}
    for key, stat in exit_stats.items():
        total = int(stat.get("total") or 0)
        filled = int(stat.get("filled") or 0)
        if total <= 0:
            continue
        p_fill_exit_buckets[key] = {
            "filled": filled,
            "total": total,
            "p": (filled + 1) / (total + 2),
        }

    converge_total = int(sum(v.get("total", 0) for v in p_converge_buckets.values()))
    converge_hit = int(sum(v.get("hit", 0) for v in p_converge_buckets.values()))
    entry_total = int(sum(v.get("total", 0) for v in p_fill_entry_buckets.values()))
    entry_filled = int(sum(v.get("filled", 0) for v in p_fill_entry_buckets.values()))
    exit_total = int(sum(v.get("total", 0) for v in p_fill_exit_buckets.values()))
    exit_filled_bucket = int(sum(v.get("filled", 0) for v in p_fill_exit_buckets.values()))

    p_converge_default = None
    if converge_total > 0:
        p_converge_default = max(0.0, min(1.0, (converge_hit + 1) / (converge_total + 2)))
    p_fill_entry_default = None
    if entry_total > 0:
        p_fill_entry_default = max(0.0, min(1.0, (entry_filled + 1) / (entry_total + 2)))
    p_fill_exit_default = None
    if exit_total > 0:
        p_fill_exit_default = max(0.0, min(1.0, (exit_filled_bucket + 1) / (exit_total + 2)))
    elif exit_attempts > 0:
        p_fill_exit_default = max(0.0, min(1.0, (exit_filled + 1) / (exit_attempts + 2)))

    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_iso,
        "fill_mode": args.fill_mode,
        "bins": bins,
        "p_converge_buckets": p_converge_buckets,
        "p_fill_entry_buckets": p_fill_entry_buckets,
        "p_fill_exit_buckets": p_fill_exit_buckets,
        "p_converge_default": p_converge_default,
        "p_fill_entry_default": p_fill_entry_default,
        "p_fill_exit_default": p_fill_exit_default,
        "hold_cost_params": {
            "mode": "linear",
            "k_per_hour": 0.0,
        },
        "stats": {
            "converge_samples": converge_total,
            "converge_hits": converge_hit,
            "entry_samples": entry_total,
            "entry_filled": entry_filled,
            "exit_samples": exit_total,
            "exit_filled_buckets": exit_filled_bucket,
            "exit_attempts": exit_attempts,
            "exit_filled": exit_filled,
            **(tick_window_cache.stats if tick_window_cache is not None else {}),
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out_path), "samples": payload.get("stats")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
